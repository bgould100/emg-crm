
"""Scheduled worker for contractor invoice email delivery."""

import logging
import signal
import threading

from app.services.contractor_invoice_delivery import (
    deliver_contractor_invoice_batch,
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
    """Run invoice delivery batches at regular intervals."""
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    logger.info("Contractor invoice delivery worker started.")

    while not stop_event.is_set():
        try:
            results = deliver_contractor_invoice_batch()

            if (
                results["claimed"]
                or results["expired_marked_unknown"]
                or results["processing_errors"]
            ):
                logger.info("Invoice delivery batch results: %s", results)

        except Exception:
            logger.exception("Unexpected invoice delivery worker error.")

        stop_event.wait(POLL_INTERVAL_SECONDS)

    logger.info("Contractor invoice delivery worker stopped.")


if __name__ == "__main__":
    run_worker()
