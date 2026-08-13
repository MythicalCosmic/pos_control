import base64
import binascii

from django.conf import settings

from vendor_api.auth import error, json_response


MAX_OFFSET = 2_147_483_647


def _encode(offset):
    return base64.urlsafe_b64encode(str(offset).encode()).decode().rstrip("=")


def _decode(value):
    try:
        padded = value + "=" * (-len(value) % 4)
        offset = int(base64.urlsafe_b64decode(padded).decode())
        if not 0 <= offset <= MAX_OFFSET:
            return None
        return offset
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError):
        return None


def paginate(request, queryset, serializer):
    try:
        requested = int(request.GET.get("page_size", settings.VENDOR_API_PAGE_SIZE))
    except (TypeError, ValueError):
        requested = settings.VENDOR_API_PAGE_SIZE
    size = max(1, min(requested, settings.VENDOR_API_MAX_PAGE_SIZE))
    if request.GET.get("cursor"):
        offset = _decode(request.GET["cursor"])
        if offset is None:
            return error("invalid_cursor", "Pagination cursor is invalid.", 422)
    else:
        try:
            page = max(1, int(request.GET.get("page", "1")))
        except (TypeError, ValueError):
            page = 1
        offset = (page - 1) * size
        if offset > MAX_OFFSET:
            return error("invalid_page", "Pagination page is too large.", 422)
    count = queryset.count()
    rows = list(queryset[offset : offset + size])
    return json_response(
        {
            "success": True,
            "count": count,
            "next": _encode(offset + size) if offset + size < count else None,
            "previous": _encode(max(0, offset - size)) if offset else None,
            "results": [serializer(row) for row in rows],
        }
    )


def paginate_list(request, rows, serializer=lambda value: value):
    """Paginate a materialized list used for computed-state filters."""
    try:
        requested = int(request.GET.get("page_size", settings.VENDOR_API_PAGE_SIZE))
    except (TypeError, ValueError):
        requested = settings.VENDOR_API_PAGE_SIZE
    size = max(1, min(requested, settings.VENDOR_API_MAX_PAGE_SIZE))
    if request.GET.get("cursor"):
        offset = _decode(request.GET["cursor"])
        if offset is None:
            return error("invalid_cursor", "Pagination cursor is invalid.", 422)
    else:
        try:
            offset = (max(1, int(request.GET.get("page", "1"))) - 1) * size
        except (TypeError, ValueError):
            offset = 0
        if offset > MAX_OFFSET:
            return error("invalid_page", "Pagination page is too large.", 422)
    count = len(rows)
    return json_response(
        {
            "success": True,
            "count": count,
            "next": _encode(offset + size) if offset + size < count else None,
            "previous": _encode(max(0, offset - size)) if offset else None,
            "results": [serializer(row) for row in rows[offset : offset + size]],
        }
    )
