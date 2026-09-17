"""Authenticated downloads for the two allowlisted Paperless export files.

Input: an authenticated administrator request and a fixed export basename.
Output: attachment with no-store headers, or 401/403/404 without private details.
HA media_source supports audio/video/images only, not these text/YAML exports.
Debug: verify the HTTP status with an admin token; never log request headers.
"""

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from .private_exports import export_destination


class ExportDownloadView(HomeAssistantView):
    """Keep credentials in exported configuration restricted to administrators."""

    url = "/api/paperless_kiplus/exports/{filename}"
    name = "api:paperless_kiplus:exports"
    requires_auth = True

    def __init__(self, hass):
        self.hass = hass

    async def get(self, request, filename):
        user = request.get("hass_user")
        if user is None:
            raise web.HTTPUnauthorized()
        if not user.is_admin:
            raise web.HTTPForbidden()
        try:
            # Resolve potentially symlinked paths on HA's file executor.
            path, _ = await self.hass.async_add_executor_job(
                export_destination, self.hass, filename
            )
        except ValueError:
            raise web.HTTPNotFound() from None
        if not await self.hass.async_add_executor_job(path.is_file):
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'attachment; filename="{filename}"',
        })
