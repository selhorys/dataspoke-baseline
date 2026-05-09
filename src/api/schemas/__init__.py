"""API schema barrel exports."""

from src.api.schemas.common import (
    ErrorResponse,
    PaginatedResponse,
    PaginationParams,
    SingleResponse,
    TimeRangeParams,
)
from src.api.schemas.dataset import (
    DatasetAttributesResponse,
    DatasetListResponse,
    DatasetResponse,
)
from src.api.schemas.events import EventListResponse, EventResponse
from src.api.schemas.ingestion import (
    CreateIngestionConfigRequest,
    IngestionConfigListResponse,
    IngestionConfigResponse,
    PatchIngestionConfigRequest,
    RunIngestionRequest,
    RunResultResponse,
)
from src.api.schemas.metagen import (
    MetagenConfPatchRequest,
    MetagenConfPutRequest,
    MetagenConfResponse,
    MetagenListItem,
    MetagenListResponse,
    MetagenResultListResponse,
    MetagenResultResponse,
    MetagenRunResponse,
    ReviewResultRequest,
    RunMetagenRequest,
)
from src.api.schemas.metrics import (
    MetricAttrResponse,
    MetricDefinitionListResponse,
    MetricDefinitionResponse,
    MetricResultListResponse,
    MetricResultResponse,
    MetricRunResultResponse,
    PatchMetricConfigRequest,
    RunMetricRequest,
    UpsertMetricConfigRequest,
)
from src.api.schemas.ontogen import (
    EdgeAttrResponse,
    EdgeListResponse,
    EdgeResponse,
    NodeAttrResponse,
    NodeListResponse,
    NodeResponse,
    OntogenConfPatchRequest,
    OntogenConfPutRequest,
    OntogenConfResponse,
    OntogenRunResponse,
    ReviewRequest,
    SeedListItem,
    SeedListResponse,
    TripleAttrResponse,
    TripleListResponse,
    TripleResponse,
)
from src.api.schemas.overview import OverviewResponse, PatchOverviewRequest
from src.api.schemas.validation import (
    PatchValidationConfRequest,
    PostValidationResultRequest,
    PutValidationConfRequest,
    ValidationConfResponse,
    ValidationListItem,
    ValidationListResponse,
    ValidationResultListResponse,
    ValidationResultRow,
)

__all__ = [
    # common
    "ErrorResponse",
    "PaginatedResponse",
    "PaginationParams",
    "SingleResponse",
    "TimeRangeParams",
    # dataset
    "DatasetAttributesResponse",
    "DatasetListResponse",
    "DatasetResponse",
    # events
    "EventListResponse",
    "EventResponse",
    # ingestion
    "CreateIngestionConfigRequest",
    "IngestionConfigListResponse",
    "IngestionConfigResponse",
    "PatchIngestionConfigRequest",
    "RunIngestionRequest",
    "RunResultResponse",
    # metagen
    "MetagenConfPatchRequest",
    "MetagenConfPutRequest",
    "MetagenConfResponse",
    "MetagenListItem",
    "MetagenListResponse",
    "MetagenResultListResponse",
    "MetagenResultResponse",
    "MetagenRunResponse",
    "ReviewResultRequest",
    "RunMetagenRequest",
    # metrics
    "MetricAttrResponse",
    "MetricDefinitionListResponse",
    "MetricDefinitionResponse",
    "MetricResultListResponse",
    "MetricResultResponse",
    "MetricRunResultResponse",
    "PatchMetricConfigRequest",
    "RunMetricRequest",
    "UpsertMetricConfigRequest",
    # ontogen
    "EdgeAttrResponse",
    "EdgeListResponse",
    "EdgeResponse",
    "NodeAttrResponse",
    "NodeListResponse",
    "NodeResponse",
    "OntogenConfPatchRequest",
    "OntogenConfPutRequest",
    "OntogenConfResponse",
    "OntogenRunResponse",
    "ReviewRequest",
    "SeedListItem",
    "SeedListResponse",
    "TripleAttrResponse",
    "TripleListResponse",
    "TripleResponse",
    # overview
    "OverviewResponse",
    "PatchOverviewRequest",
    # validation
    "PatchValidationConfRequest",
    "PostValidationResultRequest",
    "PutValidationConfRequest",
    "ValidationConfResponse",
    "ValidationListItem",
    "ValidationListResponse",
    "ValidationResultListResponse",
    "ValidationResultRow",
]
