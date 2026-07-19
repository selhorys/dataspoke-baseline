"""Schemas for the peripheral display-link surface.

Carries only the browser-facing display links the app shell needs.  This is a
non-Admin surface, so it deliberately excludes every infrastructure-topology
field of the underlying peripheral configs (``gms_url``, ``kafka_brokers``,
``service_corpuser_urn``).
"""

from typing import Annotated

from pydantic import Field

from src.api.schemas.common import (
    SAFE_DISPLAY_URL_MAX_LENGTH,
    SAFE_DISPLAY_URL_PATTERN,
    SAFE_PROJECT_ID_MAX_LENGTH,
    SAFE_PROJECT_ID_PATTERN,
    SingleResponse,
)


class PeripheralLinksResponse(SingleResponse):
    """Display links resolved from ``peripheral_config``.

    Each field is ``""`` when its peripheral is unconfigured, the underlying key
    is unset, or the stored value fails its safety check; clients read empty as
    "render no link".

    The constraints are declared on the *response* as well as on the admin
    request schema deliberately.  ``peripheral_config.settings`` is untyped
    JSONB, so a row written by direct SQL or by a caller bypassing the admin
    schema could otherwise put an attacker-controlled URL scheme straight into
    a browser ``href``.  The router coerces offending values to ``""`` before
    they reach this model, so these patterns are a backstop that keeps the
    guarantee true regardless of how the row was written.
    """

    datahub_url: Annotated[
        str,
        Field(
            default="",
            max_length=SAFE_DISPLAY_URL_MAX_LENGTH,
            pattern=SAFE_DISPLAY_URL_PATTERN,
            description=(
                "Browser-facing DataHub UI base URL, from the DataHub peripheral's"
                " `frontend_url`. Never the GMS service endpoint."
            ),
        ),
    ] = ""
    langfuse_url: Annotated[
        str,
        Field(
            default="",
            max_length=SAFE_DISPLAY_URL_MAX_LENGTH,
            pattern=SAFE_DISPLAY_URL_PATTERN,
            description="Langfuse UI base URL, from the Langfuse peripheral's `host`.",
        ),
    ] = ""
    langfuse_project_id: Annotated[
        str,
        Field(
            default="",
            max_length=SAFE_PROJECT_ID_MAX_LENGTH,
            pattern=SAFE_PROJECT_ID_PATTERN,
            description="Langfuse project id, from the Langfuse peripheral's `project_id`.",
        ),
    ] = ""
