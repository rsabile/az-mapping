"""Subscription creation date retrieval from Azure ARM APIs.

This module tries two strategies to determine when a subscription was created:

1. **Alias API** – ``Microsoft.Subscription/subscriptions/{id}`` returns a
   ``properties.createdDate`` field (requires Subscription Reader or similar).
2. **Resource-group fallback** – lists all resource groups and picks the
   oldest ``createdTime`` as an estimate.

If both strategies fail the result indicates ``source="unknown"``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TypedDict

import requests

AZURE_MGMT_URL = "https://management.azure.com"
_ALIAS_API_VERSION = "2021-10-01"
_RG_API_VERSION = "2021-04-01"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class SubscriptionAgeResult(TypedDict):
    created_date: str | None
    source: str
    is_estimated: bool
    age_days: int | None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _try_alias_api(
    subscription_id: str,
    headers: dict[str, str],
) -> str | None:
    """Attempt to retrieve the subscription creation date via the Alias API.

    Returns an ISO-8601 date string or ``None`` on failure.
    """
    url = (
        f"{AZURE_MGMT_URL}/providers/Microsoft.Subscription"
        f"/subscriptions/{subscription_id}"
        f"?api-version={_ALIAS_API_VERSION}"
    )
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 403:
            logger.warning(
                "Access denied (403) for Subscription Alias API on %s",
                subscription_id,
            )
            return None
        if resp.status_code == 404:
            logger.info(
                "Subscription Alias API returned 404 for %s",
                subscription_id,
            )
            return None
        resp.raise_for_status()
        data = resp.json()
        created = data.get("properties", {}).get("createdDate")
        if created:
            return str(created)
    except Exception:
        logger.warning("Failed to call Subscription Alias API for %s", subscription_id)
    return None


def _try_oldest_resource_group(
    subscription_id: str,
    headers: dict[str, str],
) -> str | None:
    """Fall back to listing resource groups and returning the oldest creation time.

    Returns an ISO-8601 datetime string or ``None``.
    """
    url = (
        f"{AZURE_MGMT_URL}/subscriptions/{subscription_id}"
        f"/resourcegroups?api-version={_RG_API_VERSION}"
    )
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code in (403, 404):
            logger.warning(
                "Cannot list resource groups (%s) for %s",
                resp.status_code,
                subscription_id,
            )
            return None
        resp.raise_for_status()
        rgs = resp.json().get("value", [])
        if not rgs:
            return None

        oldest: str | None = None
        for rg in rgs:
            created_time = (rg.get("properties") or {}).get("createdTime")
            if created_time and (oldest is None or created_time < oldest):
                oldest = created_time
        return oldest
    except Exception:
        logger.warning("Failed to list resource groups for %s", subscription_id)
    return None


def _compute_age_days(date_str: str) -> int | None:
    """Return the number of days between *date_str* and now (UTC)."""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(date_str, fmt).replace(tzinfo=UTC)
            delta = datetime.now(tz=UTC) - dt
            return max(delta.days, 0)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Main function
# ---------------------------------------------------------------------------


def get_subscription_creation_date(
    *,
    subscription_id: str,
    headers: dict[str, str],
) -> SubscriptionAgeResult:
    """Determine the creation date of an Azure subscription.

    Tries the Alias API first, then falls back to the oldest resource-group
    creation time.  Returns a :class:`SubscriptionAgeResult` dict.
    """
    # Strategy 1: Alias API
    alias_date = _try_alias_api(subscription_id, headers)
    if alias_date:
        return SubscriptionAgeResult(
            created_date=alias_date,
            source="alias_api",
            is_estimated=False,
            age_days=_compute_age_days(alias_date),
        )

    # Strategy 2: oldest resource group
    rg_date = _try_oldest_resource_group(subscription_id, headers)
    if rg_date:
        return SubscriptionAgeResult(
            created_date=rg_date,
            source="resource_group",
            is_estimated=True,
            age_days=_compute_age_days(rg_date),
        )

    # Both failed
    return SubscriptionAgeResult(
        created_date=None,
        source="unknown",
        is_estimated=False,
        age_days=None,
    )
