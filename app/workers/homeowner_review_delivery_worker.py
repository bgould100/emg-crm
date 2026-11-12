
"""Scheduled worker for homeowner review invitation email delivery."""

import logging
import signal
import threading

from app.services.homeowner_review_delivery import (
    deliver_homeowner_review_invitations,
    expire_homeowner_review_invitations,
)

POLL_INTERVAL_SECONDS = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)
stop_event = threading.Event()


def request_shutdown(signum, frame):
    """Request a graceful worker shutdown."""
    logger.info("Shutdown signal received.")
    stop_event.set()


def run_worker():
    """Deliver review invitations and expire overdue invitations."""
    stop_event.clear()
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    logger.info("Homeowner review delivery worker started.")

    while not stop_event.is_set():
        try:
            delivery_results = deliver_homeowner_review_invitations()

            if any(
                delivery_results.get(key)
                for key in (
                    "claimed_count",
                    "sent_count",
                    "failed_count",
                    "paused_invitation_ids",
                    "claim_lost_invitation_ids",
                    "unknown_invitation_ids",
                    "recovered_unknown_invitation_ids",
                )
            ):
                logger.info(
                    "Homeowner review delivery batch: claimed=%s sent=%s "
                    "failed=%s paused=%s claim_lost=%s unknown=%s "
                    "recovered_unknown=%s",
                    delivery_results.get("claimed_count", 0),
                    delivery_results.get("sent_count", 0),
                    delivery_results.get("failed_count", 0),
                    len(delivery_results.get("paused_invitation_ids", [])),
                    len(delivery_results.get("claim_lost_invitation_ids", [])),
                    len(delivery_results.get("unknown_invitation_ids", [])),
                    len(delivery_results.get("recovered_unknown_invitation_ids", [])),
                )

        except Exception:
            logger.exception(
                "Unexpected homeowner review delivery worker error."
            )

        try:
            expiration_results = expire_homeowner_review_invitations()

            if expiration_results["expired_count"]:
                logger.info(
                    "Homeowner review invitations expired: count=%s",
                    expiration_results["expired_count"],
                )

        except Exception:
            logger.exception(
                "Unexpected homeowner review expiration worker error."
            )

        stop_event.wait(POLL_INTERVAL_SECONDS)

    logger.info("Homeowner review delivery worker stopped.")


if __name__ == "__main__":
    run_worker()
