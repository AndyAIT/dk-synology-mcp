"""Connection manager for multi-NAS Synology API access.

Handles lazy connection initialization, session management, and service
client caching so each NAS+service pair is only instantiated once.
"""

from __future__ import annotations

import sys
import threading
from typing import Any, Optional, Type

from synology_api import cloud_sync, docker_api, downloadstation, filestation
from synology_api.audiostation import AudioStation
from synology_api.base_api import BaseApi
from synology_api.core_active_backup import ActiveBackupBusiness
from synology_api.core_backup import Backup
from synology_api.core_certificate import Certificate
from synology_api.core_group import Group
from synology_api.core_package import Package
from synology_api.core_share import Share, SharePermission
from synology_api.core_sys_info import SysInfo
from synology_api.core_user import User
from synology_api.dhcp_server import DhcpServer
from synology_api.directory_server import DirectoryServer
from synology_api.drive_admin_console import AdminConsole
from synology_api.event_scheduler import EventScheduler
from synology_api.log_center import LogCenter
from synology_api.notestation import NoteStation
from synology_api.photos import Photos
from synology_api.security_advisor import SecurityAdvisor
from synology_api.snapshot import Snapshot
from synology_api.task_scheduler import TaskScheduler
from synology_api.universal_search import UniversalSearch
from synology_api.usb_copy import USBCopy
from synology_api.virtualization import Virtualization
from synology_api.vpn import VPN

from .config import NasConfig, ServerConfig

# Map of service name -> (module class, extra kwargs)
SERVICE_REGISTRY: dict[str, Type] = {
    "filestation": filestation.FileStation,
    "downloadstation": downloadstation.DownloadStation,
    "sysinfo": SysInfo,
    "cloudsync": cloud_sync.CloudSync,
    "backup": Backup,
    "activebackup": ActiveBackupBusiness,
    "package": Package,
    "user": User,
    "group": Group,
    "share": Share,
    "share_permission": SharePermission,
    "certificate": Certificate,
    "task_scheduler": TaskScheduler,
    "event_scheduler": EventScheduler,
    "photos": Photos,
    "snapshot": Snapshot,
    "virtualization": Virtualization,
    "vpn": VPN,
    "log_center": LogCenter,
    "security_advisor": SecurityAdvisor,
    "universal_search": UniversalSearch,
    "usb_copy": USBCopy,
    "audiostation": AudioStation,
    "notestation": NoteStation,
    "drive_admin": AdminConsole,
    "docker": docker_api.Docker,
    "dhcp_server": DhcpServer,
    "directory_server": DirectoryServer,
}


class ConnectionManager:
    """Manages connections to multiple Synology NAS units.

    Thread-safe, lazily creates service clients on first access.
    """

    def __init__(self, config: ServerConfig) -> None:
        self._config = config
        self._clients: dict[str, Any] = {}  # key: "{nas_name}:{service}"
        self._sessions: dict[str, Any] = {}  # one authenticated session per NAS
        self._lock = threading.Lock()

    @property
    def config(self) -> ServerConfig:
        return self._config

    def get_client(self, service: str, nas_name: Optional[str] = None) -> Any:
        """Get or create a service client for the specified NAS.

        Args:
            service: Service name (e.g., 'filestation', 'sysinfo')
            nas_name: NAS name. Uses default if not provided.

        Returns:
            Initialized synology-api client instance.

        Raises:
            ValueError: If service or NAS name is invalid.
        """
        if service not in SERVICE_REGISTRY:
            raise ValueError(
                f"Unknown service '{service}'. Available: {sorted(SERVICE_REGISTRY.keys())}"
            )

        nas_cfg = self._config.get_nas(nas_name)
        cache_key = f"{nas_cfg.name}:{service}"

        with self._lock:
            if cache_key not in self._clients:
                self._clients[cache_key] = self._create_client(service, nas_cfg)
            return self._clients[cache_key]

    def _create_client(self, service: str, nas_cfg: NasConfig) -> Any:
        """Instantiate a synology-api client for the given service and NAS."""
        cls = SERVICE_REGISTRY[service]
        kwargs: dict[str, Any] = {
            "username": nas_cfg.username,
            "password": nas_cfg.password,
            "cert_verify": nas_cfg.cert_verify,
            "dsm_version": nas_cfg.dsm_version,
        }
        if nas_cfg.quickconnect:
            # Relay-based access via Synology QuickConnect; host/port are not used.
            kwargs["quickconnect_id"] = nas_cfg.quickconnect
            # The DSM 7 Noise-handshake login is rejected (error 414) through the relay;
            # the standard auth.cgi login works on DSM 7 and is used instead.
            kwargs["dsm_version"] = min(nas_cfg.dsm_version, 6)
        else:
            kwargs.update(ip_address=nas_cfg.host, port=str(nas_cfg.port), secure=nas_cfg.secure)
        if nas_cfg.otp_code:
            kwargs["otp_code"] = nas_cfg.otp_code

        print(f"Connecting to {nas_cfg.name} ({nas_cfg.display_host}) — {service}...", file=sys.stderr)
        try:
            # synology-api keeps a process-wide BaseApi.shared_session that every client
            # reuses, even for a different NAS. Point it at this NAS's own session
            # (or None to force a fresh login) and clear it again afterwards.
            BaseApi.shared_session = self._sessions.get(nas_cfg.name)
            try:
                client = cls(**kwargs)
            finally:
                BaseApi.shared_session = None
            self._sessions[nas_cfg.name] = client.session
            print(f"  Connected: {nas_cfg.name}/{service}", file=sys.stderr)
            return client
        except Exception as e:
            # Library errors can embed the relay URL, which contains the QuickConnect ID.
            detail = type(e).__name__ if nas_cfg.quickconnect else str(e)
            raise ConnectionError(
                f"Failed to connect to {nas_cfg.name} ({nas_cfg.display_host}) for {service}: {detail}"
            ) from (None if nas_cfg.quickconnect else e)

    def disconnect_all(self) -> None:
        """Logout and clean up all cached clients."""
        with self._lock:
            for key, client in self._clients.items():
                try:
                    client.logout()
                except Exception:
                    pass
            self._clients.clear()
            self._sessions.clear()

    def disconnect_nas(self, nas_name: str) -> None:
        """Disconnect all services for a specific NAS."""
        with self._lock:
            keys_to_remove = [k for k in self._clients if k.startswith(f"{nas_name}:")]
            for key in keys_to_remove:
                try:
                    self._clients[key].logout()
                except Exception:
                    pass
                del self._clients[key]
            self._sessions.pop(nas_name, None)

    def list_connections(self) -> list[dict[str, str]]:
        """List all active connections."""
        with self._lock:
            return [
                {"nas": k.split(":")[0], "service": k.split(":")[1]}
                for k in self._clients
            ]
