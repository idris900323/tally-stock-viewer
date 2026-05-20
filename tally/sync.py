import time

import requests


def fetch_from_tally_with_retry(session, url, xml_request, timeout=120, max_retries=3, logger=None):
    """Post XML to Tally with retries and exponential backoff."""
    last_error = None
    for attempt in range(max_retries):
        try:
            if logger:
                logger.info("[Tally] Attempt %s/%s", attempt + 1, max_retries)
            response = session.post(url, data=xml_request, timeout=timeout)
            response.raise_for_status()
            if logger:
                logger.info("[Tally] Success on attempt %s", attempt + 1)
            return response
        except requests.Timeout as exc:
            last_error = exc
            wait_time = 2 ** attempt
            if logger:
                logger.warning("[Tally] Timeout on attempt %s; retrying in %ss", attempt + 1, wait_time)
            time.sleep(wait_time)
        except requests.RequestException as exc:
            last_error = exc
            wait_time = 2 ** attempt
            if logger:
                logger.warning("[Tally] Request error on attempt %s; retrying in %ss: %s", attempt + 1, wait_time, exc)
            time.sleep(wait_time)

    if last_error is not None:
        raise last_error
    raise requests.RequestException("Tally request failed")
