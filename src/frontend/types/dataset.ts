/**
 * Dataset catalog types — derived from src/api/schemas/dataset.py
 * (GET /spoke/common/data).
 */

import type { IngestionMode } from "@/types/ingestion";

/** Owning ingestion source summary for a dataset, or null when unmanaged. */
export interface DatasetListIngestion {
  source_id: string;
  name: string;
  mode: IngestionMode;
}

/** A metagen conf whose scope matches the dataset. */
export interface DatasetListMetagenConf {
  conf_id: string;
  name: string;
}

export interface DatasetListItem {
  dataset_urn: string;
  ingestion: DatasetListIngestion | null;
  metagen: DatasetListMetagenConf[];
}

export interface DatasetListResponse {
  offset: number;
  limit: number;
  total_count: number;
  datasets: DatasetListItem[];
}
