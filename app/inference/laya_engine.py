"""The Laya runtime adapter — the only engine in the system.

Wraps ``laya.Router``, verified against ``laya 0.3.5``. The public surface:

    Router(models=None, device=None, token=None, max_loaded=1, default="english",
           auto_task_detection=False, standalone_repos=False, preload=False)
    Router.predict(state, questions, model=None, task=None, lang=None) -> dict
    Router.route(state, questions=None, model=None, ...) -> RouteDecision(dict)
    Router.load(name) -> Agent            # .cfg, .device, .model, .tok
    Router.preload(names=None)            # raises max_loaded to fit
    Router.attach(name, agent)

``predict`` returns::

    {
      "model": "laya-rl-agent",
      "answers": {
        "<qid>": {"type": "choice", "choice": ..., "probabilities": {...},
                  "confidence": <float>, "action": {"act_probability": <float>}},
        "<qid>": {"type": "score", "score": <expected level>, "legend": {"0": <level>, ...},
                  "probabilities": {"0": <p>, ...}, "confidence": ..., "action": {...}},
        "<qid>": {"type": "noul", "noul": <p>, "confidence": ..., "action": {...}},
      },
      "usage": {"input_tokens": n, "output_tokens": 0},
      "routing": {"model": "english" | "multilingual" | ..., "reason": ...},
    }

Three details shape this file:

1. ``confidence`` is **not** the top probability. Laya computes it as a
   normalised Shannon entropy, ``1 - H(p) / log(k)``, over the temperature-scaled
   distribution. Laya Studio's own calibration therefore operates on
   ``confidence`` — a second-stage recalibration, distinct from the checkpoint's
   internal logit temperature.

2. ``routing["model"]`` names the checkpoint. The top-level ``model`` key is the
   internal agent class name ("laya-rl-agent"). The checkpoint is what belongs in
   provenance.

3. ``Agent.cfg["head_max_len"]`` (default 192) caps tokens per option, and Laya
   *raises* when an option does not fit rather than truncating. It is applied by
   wrapping ``Router.load`` so lazily-loaded agents get it too.
"""

from __future__ import annotations

import inspect
import logging
import os
from pathlib import Path
from time import perf_counter
from typing import Any

from .base import (
    DecisionEngine,
    DecisionResult,
    EngineUnavailable,
    QuestionTooLarge,
    normalize_result,
)

logger = logging.getLogger(__name__)

#: Checkpoints ``Router`` knows about.
ALL_CHECKPOINTS = ("english", "multilingual", "typed-decisions")

#: What the router actually chooses between when auto-routing. ``typed-decisions``
#: is only reached by an explicit ``model=`` request, so there is no point holding
#: 421M parameters resident for it.
ROUTED_CHECKPOINTS = ("english", "multilingual")


class LayaEngine(DecisionEngine):
    """Laya inference via :class:`laya.Router`."""

    name = "laya"

    def __init__(
        self,
        *,
        preload: bool = True,
        device: str = "auto",
        cache_dir: str | None = None,
        head_max_len: int | None = None,
        max_len: int | None = None,
        offline: bool = False,
        max_loaded: int | None = None,
        token: str | None = None,
        default_model: str = "english",
    ) -> None:
        self._preload = preload
        self._device = device
        self._head_max_len = head_max_len
        self._max_len = max_len
        self._max_loaded = max_loaded
        self._default_model = default_model
        self._startup_seconds: float | None = None

        self._prepare_environment(cache_dir=cache_dir, offline=offline)

        try:
            from laya import Router  # type: ignore[import-not-found]
        except Exception as exc:  # noqa: BLE001 - import failures are manifold
            raise EngineUnavailable(
                "The `laya` package is not importable.\n"
                "    pip install 'laya>=0.3.5' 'torch>=2.2' 'transformers>=4.44'\n"
                "Laya Studio has no fallback engine: it cannot serve decisions without it."
            ) from exc

        started = perf_counter()
        kwargs = self._constructor_kwargs(Router, device=device)
        # The constructor's `preload=True` loads everything in DEFAULT_MODELS,
        # including typed-decisions. We load only the routed checkpoints instead:
        # pass preload=False and call preload(names) ourselves in _warm_up.
        kwargs["preload"] = False
        if token:
            kwargs["token"] = token
        if max_loaded is not None:
            kwargs["max_loaded"] = max_loaded
        if default_model:
            kwargs["default"] = default_model

        try:
            self._router = Router(**kwargs)
        except Exception as exc:  # noqa: BLE001 - surface any runtime failure
            raise EngineUnavailable(f"Failed to construct laya.Router({kwargs}): {exc}") from exc

        self._install_head_max_len()
        self._warm_up()
        self._prime_inference()
        self._startup_seconds = perf_counter() - started

        logger.info(
            "Laya engine ready in %.1fs (device=%s, preload=%s, checkpoints=%s, head_max_len=%s)",
            self._startup_seconds,
            device,
            preload,
            self._loaded_checkpoints(),
            head_max_len,
        )

    # -- setup --------------------------------------------------------------

    @staticmethod
    def _prepare_environment(*, cache_dir: str | None, offline: bool) -> None:
        # Silence the HF tokenizer thread warning storm.
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        if cache_dir:
            os.environ["HF_HOME"] = cache_dir
            os.environ["HF_HUB_CACHE"] = cache_dir
        if offline:
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"

    @staticmethod
    def _constructor_kwargs(router_cls: type, *, device: str) -> dict[str, Any]:
        """Only pass keyword arguments the installed ``Router`` actually takes.

        The runtime has gained constructor parameters between releases; probing
        the signature means a version bump cannot break boot on a keyword that
        did not exist yet.
        """
        try:
            parameters = inspect.signature(router_cls.__init__).parameters
        except (TypeError, ValueError):  # pragma: no cover - exotic extension types
            parameters = {}

        kwargs: dict[str, Any] = {}
        # "auto" means "let Laya choose" (cuda -> mps -> cpu), which is what
        # omitting `device` does.
        if (device and device != "auto") and ("device" in parameters or not parameters):
            kwargs["device"] = device
        return kwargs

    def _warm_up(self) -> None:
        """Load the routed checkpoints up front when configured.

        ``Router(max_loaded=1)`` evicts on every language switch, and a cold load
        costs seconds against microseconds for detection. For a panel that
        alternates languages, preloading is the difference between ~35 ms and a
        multi-second stall per request.
        """
        if not self._preload:
            return
        names = list(ROUTED_CHECKPOINTS)
        if self._default_model and self._default_model not in names:
            names.append(self._default_model)
        try:
            self._router.preload(names)
        except Exception as exc:  # noqa: BLE001 - a failed preload must not be fatal
            logger.warning(
                "Could not preload %s (%s). Checkpoints will load on first use, which makes "
                "the first request per language slow.",
                names,
                exc,
            )

    def _prime_inference(self) -> None:
        """Run one throwaway forward pass.

        Measured on MPS: the first real prediction costs ~2000 ms while torch
        sets up kernels, against ~35 ms afterwards. Paying that at boot rather
        than on the operator's first click is worth a few hundred milliseconds
        of startup.
        """
        if not self._preload:
            return
        try:
            started = perf_counter()
            self._router.predict(
                {"text": "warm up"},
                {"ready": {"type": "noul", "instructions": "Is this a warm-up call?"}},
            )
            logger.debug("Primed inference in %.0f ms", (perf_counter() - started) * 1000)
        except Exception as exc:  # noqa: BLE001 - priming is an optimisation, not a requirement
            logger.debug("Inference priming skipped: %s", exc)

    def _install_head_max_len(self) -> None:
        """Raise ``head_max_len`` on every agent, including lazily-loaded ones.

        Laya's ``choice`` and ``score`` heads render each option into a fixed
        token budget and raise when one does not fit:

            ValueError: question 'x' options exceed head_max_len=192

        So this is a hard failure rather than a silent accuracy loss. Wrapping
        ``Router.load`` is the only way to cover agents built after startup.
        """
        if self._head_max_len is None:
            return

        value = int(self._head_max_len)
        original_load = self._router.load

        def load_with_budget(name: str):
            agent = original_load(name)
            self._apply_budget(agent, value)
            return agent

        self._router.load = load_with_budget  # type: ignore[method-assign]

        # Cover any agent already resident.
        for agent in list(getattr(self._router, "_agents", {}).values()):
            self._apply_budget(agent, value)

    def _apply_budget(self, agent: Any, head_max_len: int) -> None:
        """Set ``head_max_len`` (and ``max_len``) on one agent, safely.

        The sequence Laya builds is::

            [CLS] head [SEP] opt0 opt1 ... [SEP] state [SEP]

        Options are capped at 48 tokens each, then truncated to fit inside
        ``head_max_len``; the *whole* sequence is finally truncated to
        ``max_len``. If ``head_max_len`` is too close to (or above) ``max_len``,
        the trailing options fall off the end and Laya raises::

            ValueError: question 'x' options exceed head_max_len=N

        So raising the head budget without also raising the sequence limit makes
        things worse, not better — measured on laya 0.3.5: 40 options succeeded at
        the checkpoint default of 192 and *failed* at 700 against ``max_len`` 512.
        This refuses a change that would break a schema that was working.
        """
        config = getattr(agent, "cfg", None)
        if not isinstance(config, dict):
            return

        if self._max_len is not None:
            config["max_len"] = int(self._max_len)

        sequence_limit = int(config.get("max_len", 512))
        # Leave room for [CLS], the two separators, and some state.
        safe_ceiling = sequence_limit - 32
        if head_max_len > safe_ceiling:
            logger.warning(
                "Refusing head_max_len=%s: it must stay below the sequence limit of %s "
                "(this checkpoint ships %s) or Laya silently drops trailing options. "
                "Raise LAYA_STUDIO_LAYA_MAX_LEN alongside it, or shortlist the question.",
                head_max_len,
                sequence_limit,
                config.get("head_max_len"),
            )
            return

        config["head_max_len"] = head_max_len
        logger.debug("Applied max_len=%s head_max_len=%s", sequence_limit, head_max_len)

    def _loaded_checkpoints(self) -> list[str]:
        collection = getattr(self._router, "_agents", None)
        if isinstance(collection, dict):
            return sorted(str(key) for key in collection)
        return []

    # -- contract -----------------------------------------------------------

    def predict(
        self,
        state: dict[str, Any] | str,
        questions: dict[str, dict[str, Any]],
        model: str | None = None,
    ) -> DecisionResult:
        """One forward pass over the whole question bundle.

        Deliberately unserialised. Laya's ``Router`` guards model lifecycle with
        its own lock and documents that ``Agent.system_one`` is left *outside* it
        so concurrent predictions share a checkpoint rather than queueing. Every
        call builds fresh tensors and holds a local reference to its agent, so an
        LRU eviction mid-flight cannot pull the model out from under it.
        """
        started = perf_counter()

        call_kwargs: dict[str, Any] = {}
        if model and model != "auto":
            if model not in ALL_CHECKPOINTS:
                raise ValueError(
                    f"Unknown checkpoint {model!r}; expected one of {', '.join(ALL_CHECKPOINTS)}."
                )
            call_kwargs["model"] = model

        try:
            raw = self._router.predict(state, questions, **call_kwargs)
        except TypeError:
            # Signature without the ``model`` keyword.
            raw = self._router.predict(state, questions)
        except ValueError as exc:
            if "head_max_len" in str(exc):
                # Laya ran out of sequence room for the options. This is a schema
                # problem with a specific remedy, so say so rather than letting a
                # bare ValueError reach the caller.
                raise QuestionTooLarge(str(exc)) from exc
            raise

        latency_ms = (perf_counter() - started) * 1000.0

        return normalize_result(
            raw,
            engine_name=self.name,
            latency_ms=latency_ms,
            default_model=str(call_kwargs.get("model") or self._default_model),
        )

    def route(
        self,
        state: dict[str, Any] | str,
        questions: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        from .routing import routing_decision

        try:
            decision = self._router.route(state, questions or {})
        except Exception as exc:  # noqa: BLE001 - fall back rather than 500
            logger.debug("Router.route() unavailable (%s); using local script routing", exc)
            return routing_decision(state)

        # RouteDecision subclasses dict, so it is already the payload we want.
        if isinstance(decision, dict):
            return dict(decision)

        model = getattr(decision, "model", None)
        reason = getattr(decision, "reason", None)
        if model is None and reason is None:
            return routing_decision(state)
        return {"model": model or "unknown", "reason": reason or ""}

    def describe(self) -> dict[str, Any]:
        device = self._device
        agents = getattr(self._router, "_agents", {})
        if isinstance(agents, dict):
            for name in self._loaded_checkpoints():
                resolved = getattr(agents.get(name), "device", None)
                if resolved is not None:
                    device = str(resolved)
                    break

        return {
            "name": self.name,
            "available": True,
            "device": device,
            "preload": self._preload,
            "max_loaded": getattr(self._router, "max_loaded", self._max_loaded),
            "loaded_checkpoints": self._loaded_checkpoints(),
            "head_max_len": self._head_max_len,
            "startup_seconds": (
                None if self._startup_seconds is None else round(self._startup_seconds, 2)
            ),
        }

    def checkpoint_configuration(self) -> dict[str, Any]:
        """The checkpoint's own calibration and budget settings.

        Worth surfacing because Laya ships temperature scaling per
        ``(question type, option-count bucket)``. A reader of a probability should
        be able to see that it was already corrected, and by how much.
        """
        report: dict[str, Any] = {}
        agents = getattr(self._router, "_agents", {})
        if not isinstance(agents, dict):
            return report

        for name in self._loaded_checkpoints():
            config = getattr(agents.get(name), "cfg", None)
            if not isinstance(config, dict):
                continue
            report[name] = {
                "max_len": config.get("max_len"),
                "head_max_len": config.get("head_max_len"),
                "temperature": list(config.get("temperature", [])),
                "temperature_by_options": dict(config.get("temperature_by_options", {})),
            }
        return report


def default_hf_cache() -> Path:
    home = os.environ.get("HF_HOME")
    if home:
        return Path(home) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def probe_runtime() -> dict[str, Any]:
    """Cheap environment check that imports packages without downloading weights.

    Backs ``laya-studio check``.
    """
    report: dict[str, Any] = {"ok": True, "problems": []}

    try:
        import laya

        report["laya"] = getattr(laya, "__version__", "unknown")
    except Exception as exc:  # noqa: BLE001
        report["ok"] = False
        report["problems"].append(f"`laya` is not importable: {exc}")
        return report

    try:
        import torch

        report["torch"] = torch.__version__
        if torch.cuda.is_available():
            report["device"] = f"cuda ({torch.cuda.get_device_name(0)})"
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            report["device"] = "mps"
        else:
            report["device"] = "cpu"
    except Exception as exc:  # noqa: BLE001
        report["ok"] = False
        report["problems"].append(f"`torch` is not importable: {exc}")

    try:
        import transformers

        report["transformers"] = transformers.__version__
    except Exception as exc:  # noqa: BLE001
        report["ok"] = False
        report["problems"].append(f"`transformers` is not importable: {exc}")

    try:
        from laya import DEFAULT_MODELS

        report["checkpoints"] = {
            name: "/".join(str(part) for part in parts if part)
            for name, parts in DEFAULT_MODELS.items()
        }
    except Exception:  # pragma: no cover - already reported above
        pass

    configured_cache = os.environ.get("HF_HUB_CACHE") or os.environ.get("HF_HOME")
    cache = Path(configured_cache) if configured_cache else default_hf_cache()
    report["cache_dir"] = str(cache)
    report["cache_exists"] = cache.is_dir()
    return report
