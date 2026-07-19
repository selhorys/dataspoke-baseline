"""Peripheral display links for the app shell.

Serves the browser-facing links (DataHub UI, Langfuse) that the frontend renders
in its header and deep-links, sourced from the ``peripheral_config`` DB plane so
that wiring a peripheral needs no chart change and no pod rollout.

Auth: any authenticated role.  The ``/admin/*`` peripheral surface is Admin-only
and therefore cannot serve Readers and Editors; this router is guarded by
``require_authenticated`` and exposes only the three display fields.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth.dependencies import require_authenticated
from src.api.dependencies import get_db
from src.api.schemas.common import sanitize_display_url, sanitize_project_id
from src.api.schemas.peripheral_links import PeripheralLinksResponse
from src.backend.admin.peripheral_service import (
    DatahubConfigDTO,
    LangfuseConfigDTO,
    get_peripheral_config,
)

router = APIRouter(
    tags=["common/peripheral-links"],
    dependencies=[Depends(require_authenticated)],
)


@router.get("/peripheral-links", response_model=PeripheralLinksResponse)
async def get_peripheral_links(
    db: AsyncSession = Depends(get_db),
) -> PeripheralLinksResponse:
    """Return the peripheral display links for the app shell.

    ``datahub_url`` comes from the DataHub peripheral's ``frontend_url`` — the
    browser-facing UI URL, never ``gms_url``, which addresses the GMS service and
    routinely differs in host, port, and scheme.  An unconfigured peripheral
    yields ``""`` rather than a 404, which clients read as "render no link".

    Every value is re-checked on the way out: ``peripheral_config.settings`` is
    untyped JSONB, so a row written by direct SQL or by a caller that bypasses
    the admin request schema could hold a hostile URL scheme.  A value failing
    its check degrades to ``""`` — the documented "render no link" state —
    rather than being forwarded to a browser ``href``.

    Reads go through the peripheral-config service, which already applies a
    short-TTL process-level cache.
    """
    datahub_dto = await get_peripheral_config(db, "datahub")
    langfuse_dto = await get_peripheral_config(db, "langfuse")

    datahub_url = datahub_dto.frontend_url if isinstance(datahub_dto, DatahubConfigDTO) else ""
    if isinstance(langfuse_dto, LangfuseConfigDTO):
        langfuse_url = langfuse_dto.host
        langfuse_project_id = langfuse_dto.project_id
    else:
        langfuse_url = ""
        langfuse_project_id = ""

    return PeripheralLinksResponse(
        datahub_url=sanitize_display_url(datahub_url),
        langfuse_url=sanitize_display_url(langfuse_url),
        langfuse_project_id=sanitize_project_id(langfuse_project_id),
    )
