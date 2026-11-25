"""Dependencies shared by one server's handlers."""

from __future__ import annotations

from ncit_index.embeddings import EmbeddingProvider, create_embedding_provider
from ncit_index.index import LocalIndex

from .cadsr import CaDSRClient
from .config import Settings
from .evs import LICENSE_KEY_HEADER, EVSClient
from .http_client import HttpClient
from .index_adapter import LocalIndexAdapter, index_event
from .index_port import IndexPort
from .ssis import SSISClient
from .traffic_identity import capture_identity, user_agent


class Context:
    def __init__(
        self,
        settings: Settings,
        *,
        evs: EVSClient | None = None,
        cadsr: CaDSRClient | None = None,
        ssis: SSISClient | None = None,
        index: IndexPort | None = None,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self.settings = settings
        # Renders the label for the clients built below (A6.2b). On the container path the label
        # was already checked at start-up, from the same environment and metadata in one process.
        agent = user_agent(capture_identity())
        self.cadsr = cadsr or CaDSRClient(settings, user_agent=agent)
        self.ssis = ssis or SSISClient(settings, user_agent=agent)
        self.evs = evs or EVSClient(
            settings.evs_base_url,
            settings.timeout_seconds,
            max_attempts=settings.evs_max_attempts,
            retry_backoff_seconds=settings.evs_retry_backoff_seconds,
            max_response_bytes=settings.evs_max_response_bytes,
            license_key=settings.evs_license_key,
            user_agent=agent,
        )
        self.index: IndexPort = index or LocalIndexAdapter(
            LocalIndex(settings.data_dir, on_event=index_event)
        )
        self.fhir = HttpClient(
            settings.evs_fhir_base_url,
            label="EVS_FHIR",
            timeout_seconds=settings.timeout_seconds,
            max_attempts=settings.evs_max_attempts,
            retry_backoff_seconds=settings.evs_retry_backoff_seconds,
            max_response_bytes=settings.evs_max_response_bytes,
            size_bound="NCI_SIP_EVS_MAX_RESPONSE_BYTES",
            credentials={LICENSE_KEY_HEADER: settings.evs_license_key}
            if settings.evs_license_key
            else None,
            user_agent=agent,
        )
        self.embedding_provider = embedding_provider or create_embedding_provider(
            settings.embedding_provider, settings.embedding_model
        )
