"""Criação e consumo de configurações privadas sem credencial na URL."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request, Response, status
from pydantic import BaseModel, Field

from app.models.config import settings
from app.routes.stream import (
    ContentType,
    StreamIdPath,
    _get_selected_streams,
    parse_selected_sources,
    proteger_resposta_com_token,
)
from app.services.configuration_store import (
    ConfigurationCorruptError,
    ConfigurationNotFoundError,
    PrivateConfiguration,
    configuration_store,
)
from app.services.stream_aggregator import ordered_source_ids

router = APIRouter()
ConfigurationIdPath = Annotated[
    str, Path(min_length=32, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
]


class CreateConfigurationRequest(BaseModel):
    rd_token: str = Field(min_length=1, max_length=256)
    include_p2p: bool = False
    source_ids: list[str] = Field(min_length=1, max_length=10)


class CreateConfigurationResponse(BaseModel):
    config_id: str
    manifest_url: str
    expires_in_seconds: int


async def _load_configuration(config_id: str) -> PrivateConfiguration:
    try:
        return await configuration_store.get(config_id)
    except ConfigurationNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail="Configuração inexistente ou expirada."
        ) from exc
    except ConfigurationCorruptError as exc:
        raise HTTPException(
            status_code=500, detail="Configuração privada inválida. Gere uma nova."
        ) from exc


@router.post(
    "/api/configurations",
    response_model=CreateConfigurationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_configuration(
    payload: CreateConfigurationRequest, request: Request, response: Response
) -> CreateConfigurationResponse:
    token = payload.rd_token.strip()
    if not token:
        raise HTTPException(status_code=422, detail="Token Real-Debrid vazio.")
    selected = parse_selected_sources(",".join(payload.source_ids))
    source_ids = tuple(ordered_source_ids(selected))
    config_id = await configuration_store.create(
        rd_token=token,
        include_p2p=payload.include_p2p,
        source_ids=source_ids,
    )
    proteger_resposta_com_token(response)
    base_url = str(request.base_url).rstrip("/")
    return CreateConfigurationResponse(
        config_id=config_id,
        manifest_url=f"{base_url}/config/{config_id}/manifest.json",
        expires_in_seconds=settings.CONFIG_TTL_SECONDS,
    )


@router.get("/config/{config_id}/manifest.json")
async def configured_manifest(config_id: ConfigurationIdPath, response: Response):
    from app.manifest import get_manifest

    await _load_configuration(config_id)
    proteger_resposta_com_token(response)
    return get_manifest()


@router.get("/config/{config_id}/stream/{type}/{id}.json")
async def configured_streams(
    config_id: ConfigurationIdPath,
    type: ContentType,
    id: StreamIdPath,
    request: Request,
    response: Response,
) -> dict:
    private = await _load_configuration(config_id)
    proteger_resposta_com_token(response)
    return await _get_selected_streams(
        source_ids=",".join(private.source_ids),
        rd_token=private.rd_token,
        rd_config_id=config_id,
        content_type=type,
        stremio_id=id,
        request=request,
        response=None,
        include_p2p=private.include_p2p,
    )
