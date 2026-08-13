/**
 * AdminConf Zod schema and pure helpers — extracted for testability.
 *
 * Mirrors src/api/schemas/admin.py field constraints for RuntimeConf tunables.
 *
 * Spec: spec/feature/FRONTEND_BASIC.md §Admin Configurations.
 */

import { z } from "zod";
import type { RuntimeConf, RuntimeConfPatch } from "@/lib/api/types";

// ── Zod schema ────────────────────────────────────────────────────────────────

export const confSchema = z.object({
  llm_provider: z.string().min(1, "Provider is required"),
  llm_model: z.string().min(1, "Model is required"),
  llm_api_key: z.string(),
  ontogen_llm_max_iterations: z.coerce
    .number()
    .int()
    .min(1, "Min 1")
    .max(20, "Max 20"),
  ontogen_debate_max_turns: z.coerce
    .number()
    .int()
    .min(2, "Min 2")
    .max(10, "Max 10"),
  ontogen_debate_rag_k: z.coerce
    .number()
    .int()
    .min(0, "Min 0")
    .max(20, "Max 20"),
  ontogen_debate_reviewer_model: z.string(),
  metagen_llm_max_iterations: z.coerce
    .number()
    .int()
    .min(1, "Min 1")
    .max(20, "Max 20"),
  metagen_debate_max_turns: z.coerce
    .number()
    .int()
    .min(2, "Min 2")
    .max(10, "Max 10"),
  metagen_debate_rag_k: z.coerce
    .number()
    .int()
    .min(0, "Min 0")
    .max(20, "Max 20"),
  metagen_debate_reviewer_model: z.string(),
  metagen_confidence_threshold: z.coerce
    .number()
    .min(0, "Min 0.0")
    .max(1, "Max 1.0"),
  metagen_ontology_rag_node_k: z.coerce
    .number()
    .int()
    .min(0, "Min 0")
    .max(20, "Max 20"),
  metagen_ontology_rag_edge_k: z.coerce
    .number()
    .int()
    .min(0, "Min 0")
    .max(20, "Max 20"),
  metagen_ontology_rag_triple_k: z.coerce
    .number()
    .int()
    .min(0, "Min 0")
    .max(20, "Max 20"),
  stub_redis_client: z.boolean(),
  stub_llm_client: z.boolean(),
  stub_pgvector_manager: z.boolean(),
  stub_notification_service: z.boolean(),
  auth_datahub_corp_group: z.string().min(1, "Corp group is required"),
});

export type ConfFormValues = z.infer<typeof confSchema>;

// ── Helpers ───────────────────────────────────────────────────────────────────

/** Convert a RuntimeConf API response into the form default values. */
export function toFormDefaults(conf: RuntimeConf): ConfFormValues {
  return {
    llm_provider: conf.llm_provider,
    llm_model: conf.llm_model,
    llm_api_key: "",
    ontogen_llm_max_iterations: conf.ontogen_llm_max_iterations,
    ontogen_debate_max_turns: conf.ontogen_debate_max_turns,
    ontogen_debate_rag_k: conf.ontogen_debate_rag_k,
    ontogen_debate_reviewer_model: conf.ontogen_debate_reviewer_model ?? "",
    metagen_llm_max_iterations: conf.metagen_llm_max_iterations,
    metagen_debate_max_turns: conf.metagen_debate_max_turns,
    metagen_debate_rag_k: conf.metagen_debate_rag_k,
    metagen_debate_reviewer_model: conf.metagen_debate_reviewer_model ?? "",
    metagen_confidence_threshold: conf.metagen_confidence_threshold,
    metagen_ontology_rag_node_k: conf.metagen_ontology_rag_node_k,
    metagen_ontology_rag_edge_k: conf.metagen_ontology_rag_edge_k,
    metagen_ontology_rag_triple_k: conf.metagen_ontology_rag_triple_k,
    stub_redis_client: conf.stub_redis_client,
    stub_llm_client: conf.stub_llm_client,
    stub_pgvector_manager: conf.stub_pgvector_manager,
    stub_notification_service: conf.stub_notification_service,
    auth_datahub_corp_group: conf.auth_datahub_corp_group,
  };
}

/**
 * Compute the diff between form values and the loaded conf.
 * Returns only the keys whose values have changed, using the correct types
 * for the PATCH body.
 */
export function buildPatch(values: ConfFormValues, loaded: RuntimeConf): RuntimeConfPatch {
  const patch: RuntimeConfPatch = {};

  if (values.llm_provider !== loaded.llm_provider) {
    patch.llm_provider = values.llm_provider;
  }
  if (values.llm_model !== loaded.llm_model) {
    patch.llm_model = values.llm_model;
  }
  // Only include llm_api_key when the user typed something (blank means "keep current")
  if (values.llm_api_key !== "") {
    patch.llm_api_key = values.llm_api_key;
  }
  if (values.ontogen_llm_max_iterations !== loaded.ontogen_llm_max_iterations) {
    patch.ontogen_llm_max_iterations = values.ontogen_llm_max_iterations;
  }
  if (values.ontogen_debate_max_turns !== loaded.ontogen_debate_max_turns) {
    patch.ontogen_debate_max_turns = values.ontogen_debate_max_turns;
  }
  if (values.ontogen_debate_rag_k !== loaded.ontogen_debate_rag_k) {
    patch.ontogen_debate_rag_k = values.ontogen_debate_rag_k;
  }
  const loadedOntoReviewer = loaded.ontogen_debate_reviewer_model ?? "";
  if (values.ontogen_debate_reviewer_model !== loadedOntoReviewer) {
    // Empty string → send null to clear on the server
    patch.ontogen_debate_reviewer_model =
      values.ontogen_debate_reviewer_model === "" ? null : values.ontogen_debate_reviewer_model;
  }
  if (values.metagen_llm_max_iterations !== loaded.metagen_llm_max_iterations) {
    patch.metagen_llm_max_iterations = values.metagen_llm_max_iterations;
  }
  if (values.metagen_debate_max_turns !== loaded.metagen_debate_max_turns) {
    patch.metagen_debate_max_turns = values.metagen_debate_max_turns;
  }
  if (values.metagen_debate_rag_k !== loaded.metagen_debate_rag_k) {
    patch.metagen_debate_rag_k = values.metagen_debate_rag_k;
  }
  const loadedMetaReviewer = loaded.metagen_debate_reviewer_model ?? "";
  if (values.metagen_debate_reviewer_model !== loadedMetaReviewer) {
    patch.metagen_debate_reviewer_model =
      values.metagen_debate_reviewer_model === "" ? null : values.metagen_debate_reviewer_model;
  }
  if (values.metagen_confidence_threshold !== loaded.metagen_confidence_threshold) {
    patch.metagen_confidence_threshold = values.metagen_confidence_threshold;
  }
  if (values.metagen_ontology_rag_node_k !== loaded.metagen_ontology_rag_node_k) {
    patch.metagen_ontology_rag_node_k = values.metagen_ontology_rag_node_k;
  }
  if (values.metagen_ontology_rag_edge_k !== loaded.metagen_ontology_rag_edge_k) {
    patch.metagen_ontology_rag_edge_k = values.metagen_ontology_rag_edge_k;
  }
  if (values.metagen_ontology_rag_triple_k !== loaded.metagen_ontology_rag_triple_k) {
    patch.metagen_ontology_rag_triple_k = values.metagen_ontology_rag_triple_k;
  }
  if (values.stub_redis_client !== loaded.stub_redis_client) {
    patch.stub_redis_client = values.stub_redis_client;
  }
  if (values.stub_llm_client !== loaded.stub_llm_client) {
    patch.stub_llm_client = values.stub_llm_client;
  }
  if (values.stub_pgvector_manager !== loaded.stub_pgvector_manager) {
    patch.stub_pgvector_manager = values.stub_pgvector_manager;
  }
  if (values.stub_notification_service !== loaded.stub_notification_service) {
    patch.stub_notification_service = values.stub_notification_service;
  }
  if (values.auth_datahub_corp_group !== loaded.auth_datahub_corp_group) {
    patch.auth_datahub_corp_group = values.auth_datahub_corp_group;
  }

  return patch;
}
