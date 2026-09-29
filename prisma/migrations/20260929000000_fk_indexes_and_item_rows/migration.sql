-- Index every foreign-key column that had none. PostgreSQL does not index the
-- referencing side automatically, so each ON DELETE SET NULL on the parent had to
-- scan the child table (deleting evaluations, revisions or schemas), and lookups
-- by these columns (e.g. review de-duplication by evaluation_id) scanned too.
--
-- One item per row: (job_id, row_index) and (test_id, row_index) become unique,
-- which the services already assume. Those unique indexes lead with the FK column,
-- so the single-column job_id / test_id indexes go, along with the other indexes
-- that duplicate the leading column of an existing composite or unique index.

-- DropIndex
DROP INDEX "ix_schema_revisions_schema_id";

-- DropIndex
DROP INDEX "ix_evaluations_created_at";

-- DropIndex
DROP INDEX "ix_bulk_job_items_job_id";

-- DropIndex
DROP INDEX "ix_ab_test_items_test_id";

-- DropIndex
DROP INDEX "ix_workflow_runs_route";

-- DropIndex
DROP INDEX "ix_review_items_status";

-- CreateIndex
CREATE INDEX "ix_evaluations_schema_revision_id" ON "evaluations"("schema_revision_id");

-- CreateIndex
CREATE INDEX "ix_bulk_jobs_schema_id" ON "bulk_jobs"("schema_id");

-- CreateIndex
CREATE INDEX "ix_bulk_job_items_evaluation_id" ON "bulk_job_items"("evaluation_id");

-- CreateIndex
CREATE UNIQUE INDEX "uq_bulk_job_item_row" ON "bulk_job_items"("job_id", "row_index");

-- CreateIndex
CREATE INDEX "ix_ab_tests_schema_id" ON "ab_tests"("schema_id");

-- CreateIndex
CREATE INDEX "ix_ab_tests_revision_a_id" ON "ab_tests"("revision_a_id");

-- CreateIndex
CREATE INDEX "ix_ab_tests_revision_b_id" ON "ab_tests"("revision_b_id");

-- CreateIndex
CREATE UNIQUE INDEX "uq_ab_test_item_row" ON "ab_test_items"("test_id", "row_index");

-- CreateIndex
CREATE INDEX "ix_workflows_schema_id" ON "workflows"("schema_id");

-- CreateIndex
CREATE INDEX "ix_workflow_runs_evaluation_id" ON "workflow_runs"("evaluation_id");

-- CreateIndex
CREATE INDEX "ix_review_items_workflow_run_id" ON "review_items"("workflow_run_id");

-- CreateIndex
CREATE INDEX "ix_review_items_evaluation_id" ON "review_items"("evaluation_id");
