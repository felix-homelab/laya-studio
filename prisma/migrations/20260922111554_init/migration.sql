-- CreateTable
CREATE TABLE "eval_schemas" (
    "id" SERIAL NOT NULL,
    "key" TEXT NOT NULL,
    "name" TEXT NOT NULL,
    "description" TEXT NOT NULL,
    "primitive" TEXT NOT NULL,
    "definition" JSONB NOT NULL,
    "tags" JSONB NOT NULL,
    "is_template" BOOLEAN NOT NULL,
    "current_revision" INTEGER NOT NULL,
    "created_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "eval_schemas_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "schema_revisions" (
    "id" SERIAL NOT NULL,
    "schema_id" INTEGER NOT NULL,
    "revision" INTEGER NOT NULL,
    "definition" JSONB NOT NULL,
    "note" TEXT NOT NULL,
    "created_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "schema_revisions_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "evaluations" (
    "id" SERIAL NOT NULL,
    "schema_id" INTEGER,
    "schema_revision_id" INTEGER,
    "state" JSONB NOT NULL,
    "state_text" TEXT NOT NULL,
    "questions" JSONB NOT NULL,
    "answers" JSONB NOT NULL,
    "question_results" JSONB NOT NULL,
    "routing" JSONB NOT NULL,
    "engine" TEXT NOT NULL,
    "engine_model" TEXT NOT NULL,
    "primary_question" TEXT,
    "top_choice" TEXT,
    "confidence" DOUBLE PRECISION,
    "raw_confidence" DOUBLE PRECISION,
    "calibrated" BOOLEAN NOT NULL,
    "latency_ms" DOUBLE PRECISION NOT NULL,
    "source" TEXT NOT NULL,
    "label" TEXT,
    "label_source" TEXT,
    "created_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "evaluations_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "bulk_jobs" (
    "id" SERIAL NOT NULL,
    "name" TEXT NOT NULL,
    "schema_id" INTEGER,
    "filename" TEXT NOT NULL,
    "status" TEXT NOT NULL,
    "total_rows" INTEGER NOT NULL,
    "processed_rows" INTEGER NOT NULL,
    "failed_rows" INTEGER NOT NULL,
    "options" JSONB NOT NULL,
    "summary" JSONB NOT NULL,
    "error" TEXT,
    "started_at" TIMESTAMPTZ(3),
    "finished_at" TIMESTAMPTZ(3),
    "created_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "bulk_jobs_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "bulk_job_items" (
    "id" SERIAL NOT NULL,
    "job_id" INTEGER NOT NULL,
    "row_index" INTEGER NOT NULL,
    "state" JSONB NOT NULL,
    "evaluation_id" INTEGER,
    "label" TEXT,
    "predicted" TEXT,
    "confidence" DOUBLE PRECISION,
    "ok" BOOLEAN NOT NULL,
    "error" TEXT,
    "created_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "bulk_job_items_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "ab_tests" (
    "id" SERIAL NOT NULL,
    "name" TEXT NOT NULL,
    "schema_id" INTEGER,
    "revision_a_id" INTEGER,
    "revision_b_id" INTEGER,
    "status" TEXT NOT NULL,
    "dataset" JSONB NOT NULL,
    "summary" JSONB NOT NULL,
    "error" TEXT,
    "finished_at" TIMESTAMPTZ(3),
    "created_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "ab_tests_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "ab_test_items" (
    "id" SERIAL NOT NULL,
    "test_id" INTEGER NOT NULL,
    "row_index" INTEGER NOT NULL,
    "state" JSONB NOT NULL,
    "label" TEXT,
    "choice_a" TEXT,
    "choice_b" TEXT,
    "confidence_a" DOUBLE PRECISION,
    "confidence_b" DOUBLE PRECISION,
    "flipped" BOOLEAN NOT NULL,
    "result_a" JSONB NOT NULL,
    "result_b" JSONB NOT NULL,
    "created_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "ab_test_items_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "workflows" (
    "id" SERIAL NOT NULL,
    "key" TEXT NOT NULL,
    "name" TEXT NOT NULL,
    "description" TEXT NOT NULL,
    "enabled" BOOLEAN NOT NULL,
    "trigger_type" TEXT NOT NULL,
    "trigger_config" JSONB NOT NULL,
    "schema_id" INTEGER,
    "decision_question" TEXT,
    "steps" JSONB NOT NULL,
    "auto_threshold" DOUBLE PRECISION NOT NULL,
    "verify_threshold" DOUBLE PRECISION NOT NULL,
    "created_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "workflows_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "workflow_runs" (
    "id" SERIAL NOT NULL,
    "workflow_id" INTEGER NOT NULL,
    "evaluation_id" INTEGER,
    "status" TEXT NOT NULL,
    "route" TEXT,
    "confidence" DOUBLE PRECISION,
    "final_choice" TEXT,
    "actions" JSONB NOT NULL,
    "trigger_payload" JSONB NOT NULL,
    "error" TEXT,
    "started_at" TIMESTAMPTZ(3),
    "finished_at" TIMESTAMPTZ(3),
    "created_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "workflow_runs_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "action_logs" (
    "id" SERIAL NOT NULL,
    "workflow_run_id" INTEGER,
    "kind" TEXT NOT NULL,
    "target" TEXT NOT NULL,
    "status" TEXT NOT NULL,
    "request" JSONB NOT NULL,
    "response" JSONB NOT NULL,
    "error" TEXT,
    "latency_ms" DOUBLE PRECISION NOT NULL,
    "created_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "action_logs_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "review_items" (
    "id" SERIAL NOT NULL,
    "workflow_id" INTEGER,
    "workflow_run_id" INTEGER,
    "evaluation_id" INTEGER,
    "state" JSONB NOT NULL,
    "state_text" TEXT NOT NULL,
    "question" TEXT,
    "model_choice" TEXT,
    "model_confidence" DOUBLE PRECISION,
    "alternatives" JSONB NOT NULL,
    "reason" TEXT NOT NULL,
    "priority" INTEGER NOT NULL,
    "status" TEXT NOT NULL,
    "assignee" TEXT,
    "corrected_label" TEXT,
    "notes" TEXT NOT NULL,
    "resolved_at" TIMESTAMPTZ(3),
    "created_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "review_items_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "calibration_snapshots" (
    "id" SERIAL NOT NULL,
    "primitive" TEXT NOT NULL,
    "question" TEXT NOT NULL,
    "schema_id" INTEGER,
    "n_samples" INTEGER NOT NULL,
    "slope" DOUBLE PRECISION NOT NULL,
    "intercept" DOUBLE PRECISION NOT NULL,
    "temperature" DOUBLE PRECISION,
    "ece" DOUBLE PRECISION,
    "mce" DOUBLE PRECISION,
    "brier" DOUBLE PRECISION,
    "accuracy" DOUBLE PRECISION,
    "mean_confidence" DOUBLE PRECISION,
    "bins" JSONB NOT NULL,
    "created_at" TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "calibration_snapshots_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE UNIQUE INDEX "ix_eval_schemas_key" ON "eval_schemas"("key");

-- CreateIndex
CREATE INDEX "ix_eval_schemas_created_at" ON "eval_schemas"("created_at");

-- CreateIndex
CREATE INDEX "ix_schema_revisions_schema_id" ON "schema_revisions"("schema_id");

-- CreateIndex
CREATE UNIQUE INDEX "uq_schema_revision" ON "schema_revisions"("schema_id", "revision");

-- CreateIndex
CREATE INDEX "ix_evaluations_created_at" ON "evaluations"("created_at");

-- CreateIndex
CREATE INDEX "ix_evaluations_top_choice" ON "evaluations"("top_choice");

-- CreateIndex
CREATE INDEX "ix_evaluations_source" ON "evaluations"("source");

-- CreateIndex
CREATE INDEX "ix_evaluations_schema_id" ON "evaluations"("schema_id");

-- CreateIndex
CREATE INDEX "ix_evaluations_confidence" ON "evaluations"("confidence");

-- CreateIndex
CREATE INDEX "ix_evaluations_created_source" ON "evaluations"("created_at", "source");

-- CreateIndex
CREATE INDEX "ix_bulk_jobs_created_at" ON "bulk_jobs"("created_at");

-- CreateIndex
CREATE INDEX "ix_bulk_jobs_status" ON "bulk_jobs"("status");

-- CreateIndex
CREATE INDEX "ix_bulk_job_items_job_id" ON "bulk_job_items"("job_id");

-- CreateIndex
CREATE INDEX "ix_ab_tests_created_at" ON "ab_tests"("created_at");

-- CreateIndex
CREATE INDEX "ix_ab_tests_status" ON "ab_tests"("status");

-- CreateIndex
CREATE INDEX "ix_ab_test_items_test_id" ON "ab_test_items"("test_id");

-- CreateIndex
CREATE UNIQUE INDEX "ix_workflows_key" ON "workflows"("key");

-- CreateIndex
CREATE INDEX "ix_workflows_created_at" ON "workflows"("created_at");

-- CreateIndex
CREATE INDEX "ix_workflows_enabled" ON "workflows"("enabled");

-- CreateIndex
CREATE INDEX "ix_workflow_runs_created_at" ON "workflow_runs"("created_at");

-- CreateIndex
CREATE INDEX "ix_workflow_runs_route" ON "workflow_runs"("route");

-- CreateIndex
CREATE INDEX "ix_workflow_runs_route_created" ON "workflow_runs"("route", "created_at");

-- CreateIndex
CREATE INDEX "ix_workflow_runs_status" ON "workflow_runs"("status");

-- CreateIndex
CREATE INDEX "ix_workflow_runs_workflow_id" ON "workflow_runs"("workflow_id");

-- CreateIndex
CREATE INDEX "ix_action_logs_created_at" ON "action_logs"("created_at");

-- CreateIndex
CREATE INDEX "ix_action_logs_kind" ON "action_logs"("kind");

-- CreateIndex
CREATE INDEX "ix_action_logs_status" ON "action_logs"("status");

-- CreateIndex
CREATE INDEX "ix_action_logs_workflow_run_id" ON "action_logs"("workflow_run_id");

-- CreateIndex
CREATE INDEX "ix_review_items_created_at" ON "review_items"("created_at");

-- CreateIndex
CREATE INDEX "ix_review_items_priority" ON "review_items"("priority");

-- CreateIndex
CREATE INDEX "ix_review_items_status" ON "review_items"("status");

-- CreateIndex
CREATE INDEX "ix_review_items_status_priority" ON "review_items"("status", "priority");

-- CreateIndex
CREATE INDEX "ix_review_items_workflow_id" ON "review_items"("workflow_id");

-- CreateIndex
CREATE INDEX "ix_calibration_snapshots_created_at" ON "calibration_snapshots"("created_at");

-- CreateIndex
CREATE INDEX "ix_calibration_snapshots_primitive" ON "calibration_snapshots"("primitive");

-- CreateIndex
CREATE INDEX "ix_calibration_snapshots_question" ON "calibration_snapshots"("question");

-- CreateIndex
CREATE INDEX "ix_calibration_snapshots_schema_id" ON "calibration_snapshots"("schema_id");

-- AddForeignKey
ALTER TABLE "schema_revisions" ADD CONSTRAINT "schema_revisions_schema_id_fkey" FOREIGN KEY ("schema_id") REFERENCES "eval_schemas"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "evaluations" ADD CONSTRAINT "evaluations_schema_id_fkey" FOREIGN KEY ("schema_id") REFERENCES "eval_schemas"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "evaluations" ADD CONSTRAINT "evaluations_schema_revision_id_fkey" FOREIGN KEY ("schema_revision_id") REFERENCES "schema_revisions"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "bulk_jobs" ADD CONSTRAINT "bulk_jobs_schema_id_fkey" FOREIGN KEY ("schema_id") REFERENCES "eval_schemas"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "bulk_job_items" ADD CONSTRAINT "bulk_job_items_job_id_fkey" FOREIGN KEY ("job_id") REFERENCES "bulk_jobs"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "bulk_job_items" ADD CONSTRAINT "bulk_job_items_evaluation_id_fkey" FOREIGN KEY ("evaluation_id") REFERENCES "evaluations"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ab_tests" ADD CONSTRAINT "ab_tests_schema_id_fkey" FOREIGN KEY ("schema_id") REFERENCES "eval_schemas"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ab_tests" ADD CONSTRAINT "ab_tests_revision_a_id_fkey" FOREIGN KEY ("revision_a_id") REFERENCES "schema_revisions"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ab_tests" ADD CONSTRAINT "ab_tests_revision_b_id_fkey" FOREIGN KEY ("revision_b_id") REFERENCES "schema_revisions"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ab_test_items" ADD CONSTRAINT "ab_test_items_test_id_fkey" FOREIGN KEY ("test_id") REFERENCES "ab_tests"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "workflows" ADD CONSTRAINT "workflows_schema_id_fkey" FOREIGN KEY ("schema_id") REFERENCES "eval_schemas"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "workflow_runs" ADD CONSTRAINT "workflow_runs_workflow_id_fkey" FOREIGN KEY ("workflow_id") REFERENCES "workflows"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "workflow_runs" ADD CONSTRAINT "workflow_runs_evaluation_id_fkey" FOREIGN KEY ("evaluation_id") REFERENCES "evaluations"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "action_logs" ADD CONSTRAINT "action_logs_workflow_run_id_fkey" FOREIGN KEY ("workflow_run_id") REFERENCES "workflow_runs"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "review_items" ADD CONSTRAINT "review_items_workflow_id_fkey" FOREIGN KEY ("workflow_id") REFERENCES "workflows"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "review_items" ADD CONSTRAINT "review_items_workflow_run_id_fkey" FOREIGN KEY ("workflow_run_id") REFERENCES "workflow_runs"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "review_items" ADD CONSTRAINT "review_items_evaluation_id_fkey" FOREIGN KEY ("evaluation_id") REFERENCES "evaluations"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "calibration_snapshots" ADD CONSTRAINT "calibration_snapshots_schema_id_fkey" FOREIGN KEY ("schema_id") REFERENCES "eval_schemas"("id") ON DELETE SET NULL ON UPDATE CASCADE;
