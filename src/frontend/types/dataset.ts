/**
 * Dataset catalog types — derived from src/api/schemas/dataset.py
 * (GET /spoke/common/data).
 */

import type { IngestionMode } from "@/types/ingestion";

/** One ingestion source covering a dataset in the catalog list. */
export interface DatasetListIngestion {
  source_id: string;
  name: string;
  mode: IngestionMode;
  platform: string;
}

/** Validation-coverage summary for a dataset in the catalog list. */
export interface DatasetListValidation {
  covered: boolean;
}

/** A metagen conf whose scope matches the dataset. */
export interface DatasetListMetagenConf {
  conf_id: string;
  name: string;
}

export interface DatasetListItem {
  dataset_urn: string;
  ingestion: DatasetListIngestion[];
  validation: DatasetListValidation;
  metagen: DatasetListMetagenConf[];
}

export interface DatasetListResponse {
  offset: number;
  limit: number;
  total_count: number;
  datasets: DatasetListItem[];
}
