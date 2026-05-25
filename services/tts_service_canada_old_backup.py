from __future__ import annotations

import os
import json
import subprocess
import time
from typing import Optional, Tuple, Dict

import requests
from requests.exceptions import RequestException


def _build_requests_proxies(proxy_cfg: Optional[Dict]) -> Optional[Dict[str, str]]:
    """
    Build requests-compatible proxies dict from proxy_cfg:
      proxy_cfg = {host, port, username, password}

    Returns:
      {"http": "http://user:pass@host:port", "https": "http://user:pass@host:port"}
      or None if proxy_cfg is empty/invalid.
    """
    if not proxy_cfg:
        return None

    host = proxy_cfg.get("host")
    port = proxy_cfg.get("port")
    if not host or not port:
        return None

    try:
        port_int = int(str(port))
        if not (0 < port_int <= 65535):
            return None
    except (ValueError, TypeError):
        return None

    user = proxy_cfg.get("username") or ""
    pwd = proxy_cfg.get("password") or ""

    if user:
        proxy_url = f"http://{user}:{pwd}@{host}:{port_int}"
    else:
        proxy_url = f"http://{host}:{port_int}"

    # Chỉ dùng "http" như file test thành công (100% không bị 401)
    # File test: proxies = {"http": proxy} - không có "https"
    return {"http": proxy_url,"https": proxy_url}


def create_job_via_canada_proxy(voice_id: str, api_key: str, payload_json: str, proxy_cfg: dict) -> Tuple[bool, str, str, Optional[str], bool, bool]:
    """
    Phase 1: Gửi TTS request qua PROXY - chỉ gửi text, không download audio
    Giống tool_dgt.py: chỉ lấy history-item-id từ headers rồi đóng connection ngay
    
    Returns: (success, error_info, request_id, history_item_id, is_quota_exceeded, is_voice_limit_reached)
    """
    import logging
    logger = logging.getLogger('tts_service')

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    
    # Validate JSON payload
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as e:
        logger.error(f"CANADA_PHASE1_JSON_INVALID - Payload JSON is invalid: {e}")
        logger.error(f"CANADA_PHASE1_JSON_PREVIEW - First 200 chars: {payload_json[:200]}")
        return False, f"Invalid JSON payload: {e}", None, None, False, False

    proxies = _build_requests_proxies(proxy_cfg)

    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",  # we expect audio back
    }

    max_retries = 2
    resp = None
    err_text = ""
    
    for attempt in range(max_retries + 1):
        try:
            # Phase 1: Gửi request qua proxy, dùng stream=True để có thể đóng connection sớm
            # PHASE 1: Reduced timeout (5s) for fast fail/rotation
            timeout = (30, 30)
            resp = requests.post(
                url,
                headers=headers,
                json=payload,
                proxies=proxies,
                timeout=timeout,
                stream=True,
            )
            break
        except (requests.ConnectTimeout, requests.ReadTimeout) as e:
            # OPTIMIZATION: Fail FAST on timeout (user request)
            # Don't retry timeouts - assume proxy is dead/stuck and needs rotation immediately
            logger.warning(f"CANADA_PHASE1_TIMEOUT_FAST_FAIL - attempt {attempt+1}/{max_retries+1}: {e}")
            return False, f"TIMEOUT_ERROR: {e}", None, None, False, False
            
        except RequestException as e:
            err_text = str(e)
            logger.warning(f"CANADA_PHASE1_REQUEST_EXCEPTION attempt {attempt+1}/{max_retries+1}: {e}")
            
            # Simple retry for non-timeout errors: Only fail after max_retries
            if attempt < max_retries:
                time.sleep(1.0)  # Brief pause before retry
                continue
            
            # All retries exhausted - now trigger rotation
            logger.error(f"CANADA_PHASE1_ALL_RETRIES_FAILED - Triggering proxy rotation")
            return False, f"REQUEST_ERROR: {e}", None, None, False, False

    if resp is None:
        return False, f"REQUEST_ERROR: {err_text}", None, None, False, False

    info = f"HTTP_{resp.status_code}"
    request_id = resp.headers.get("request-id") or resp.headers.get("x-request-id")
    history_item_id = resp.headers.get("history-item-id")

    is_quota_exceeded = False
    is_voice_limit_reached = False

    # Check for errors first
    if resp.status_code in (401, 429, 403, 503):
        # Đóng connection trước khi parse error
        try:
            resp.raw._fp.close()
        except:
            pass
        resp.close()
        
        # Parse error response
        try:
            body_text = resp.text or ""
            err_json = resp.json()
            detail = err_json.get("detail")
            status = None
            message = None
            if isinstance(detail, dict):
                status = detail.get("status")
                message = detail.get("message") or detail.get("error")
            elif isinstance(detail, str):
                message = detail
            status = status or err_json.get("status")
            
            low = json.dumps(err_json).lower()
            is_quota_exceeded = "quota_exceeded" in low or "exceeds your quota" in low
            is_voice_limit_reached = "voice_limit_reached" in low or "maximum amount of custom voices" in low
            
            if status:
                info = status
            if message:
                info = f"{info}: {message}"
            if is_quota_exceeded:
                info = "QUOTA_EXCEEDED"
            if is_voice_limit_reached:
                info = "VOICE_LIMIT_REACHED"
        except:
            body_text = resp.text or ""
            low = (body_text or "").lower()
            is_quota_exceeded = ("quota_exceeded" in low or "exceeds your quota" in low)
            is_voice_limit_reached = ("voice_limit_reached" in low or "maximum amount of custom voices" in low)
            if is_quota_exceeded:
                info = f"QUOTA_EXCEEDED: {body_text[:200]}"
            elif is_voice_limit_reached:
                info = f"VOICE_LIMIT_REACHED: {body_text[:200]}"
            else:
                info = f"{info}: {body_text[:200]}"
        
        logger.error(f"CANADA_PHASE1_ERROR - http={resp.status_code}, info={info}")
        return False, info, None, None, is_quota_exceeded, is_voice_limit_reached

    # Success path - Phase 1: CHỈ lấy history-id từ headers rồi ĐÓNG NGAY
    if resp.status_code == 200:
        if not history_item_id:
            # Đóng connection trước khi return error
            try:
                resp.raw._fp.close()
            except:
                pass
            resp.close()
            logger.error("CANADA_PHASE1_NO_HISTORY_ID - 200 OK nhưng không có history-item-id trong headers")
            return False, "No history-item-id in headers", None, None, False, False
        
        # === PHASE 1: CHỈ lấy history-id từ headers rồi ĐÓNG NGAY ===
        # ĐÓNG CONNECTION NGAY LẬP TỨC - không đọc audio
        # (Phase 2 có retry nếu history chưa sẵn sàng)
        try:
            resp.raw._fp.close()
        except:
            pass
        resp.close()
        
        logger.info(f"CANADA_PHASE1_SUCCESS - request_id={request_id}, history_item_id={history_item_id}")
        return True, info, request_id, history_item_id, False, False

    # Nếu đến đây thì có lỗi không xác định
    # Đóng connection trước khi return
    try:
        resp.raw._fp.close()
    except:
        pass
    resp.close()
    
    logger.error(f"CANADA_PHASE1_UNKNOWN_ERROR - http={resp.status_code}")
    return False, f"HTTP_{resp.status_code}", None, None, False, False


def _fetch_custom_voice_candidates(api_key: str, logger) -> list[dict]:
    """
    Fetch candidate custom voices that can be deleted in order to free up slots.
    Uses `requests` instead of curl for all HTTP calls.
    """
    import requests
    from requests.exceptions import RequestException

    endpoints = [
        {
            "name": "v2/non-default",
            "url": "https://api.elevenlabs.io/v2/voices",
            "params": {
                "voice_type": "non-default",
                "include_total_count": "false",
                "page_size": "100",
            },
        },
        {
            "name": "v2/custom",
            "url": "https://api.elevenlabs.io/v2/voices",
            "params": {
                "voice_type": "custom",
                "include_total_count": "false",
                "page_size": "100",
            },
        },
        {
            "name": "v1/all",
            "url": "https://api.elevenlabs.io/v1/voices",
            "params": {},
        },
    ]

    headers = {
        "xi-api-key": api_key,
    }

    candidates: list[dict] = []
    seen_ids: set[str] = set()

    for endpoint in endpoints:
        try:
            resp = requests.get(
                endpoint["url"],
                headers=headers,
                params=endpoint.get("params") or None,
                timeout=(10, 15),
            )
        except RequestException as exc:
            logger.warning(f"CANADA_VOICE_LIST_{endpoint['name'].upper()} - Exception during request: {exc}")
            continue

        if resp.status_code != 200:
            logger.warning(
                f"CANADA_VOICE_LIST_{endpoint['name'].upper()} - HTTP {resp.status_code}: {(resp.text or '')[:200]}"
            )
            continue

        try:
            data = resp.json() or {}
        except ValueError as exc:
            logger.warning(f"CANADA_VOICE_LIST_{endpoint['name'].upper()} - JSON parse failed: {exc}")
            continue

        if isinstance(data, list):
            voice_list = data
        else:
            voice_list = data.get("voices") if isinstance(data, dict) else []

        if not voice_list:
            logger.info(f"CANADA_VOICE_LIST_{endpoint['name'].upper()} - No voices returned")
            continue

        for voice in voice_list:
            if not isinstance(voice, dict):
                continue

            voice_id = voice.get("voice_id") or voice.get("id")
            if not voice_id or voice_id in seen_ids:
                continue

            category = (voice.get("category") or "").lower()
            labels = voice.get("labels") or {}
            is_owner = bool(voice.get("is_owner"))

            # Heuristic: treat as custom if explicitly custom OR owned OR labelled custom/clone
            is_custom = (
                category in ("custom", "cloned", "uploaded")
                or labels.get("is_custom")
                or labels.get("is_cloned")
                or is_owner
                or voice.get("voice_type") == "custom"
            )

            if is_custom:
                seen_ids.add(voice_id)
                candidates.append(
                    {
                        "voice_id": voice_id,
                        "name": voice.get("name", "Unknown"),
                        "category": category,
                        "labels": labels,
                        "created_at": voice.get("created_at") or voice.get("date"),
                        "last_used": voice.get("last_used") or voice.get("updated_at"),
                        "source_endpoint": endpoint["name"],
                    }
                )

    return candidates


def _try_delete_one_voice(api_key: str, logger) -> bool:
    """
    Try to delete one non-default voice to free up a voice slot.
    Returns True if a voice was successfully deleted, False otherwise.
    """
    try:
        candidates = _fetch_custom_voice_candidates(api_key, logger)
        if not candidates:
            logger.warning("CANADA_VOICE_DELETE_NO_VOICES - No custom voices found to delete")
            return False

        # Prefer oldest/least recently used voice (deterministic fallback)
        def sort_key(voice: dict) -> tuple:
            return (
                voice.get("created_at") or "",
                voice.get("last_used") or "",
                voice.get("name") or "",
                voice.get("voice_id") or "",
            )

        voice_to_delete = sorted(candidates, key=sort_key)[0]
        voice_id = voice_to_delete.get("voice_id")
        voice_name = voice_to_delete.get("name", "Unknown")
        source_endpoint = voice_to_delete.get("source_endpoint")

        if not voice_id:
            logger.error("CANADA_VOICE_DELETE_NO_ID - Candidate voice missing voice_id")
            return False

        logger.info(
            f"CANADA_VOICE_DELETE_ATTEMPT - Deleting voice {voice_name} ({voice_id[:10]}...) from {source_endpoint}"
        )

        import requests
        from requests.exceptions import RequestException

        try:
            resp = requests.delete(
            f"https://api.elevenlabs.io/v1/voices/{voice_id}",
                headers={"xi-api-key": api_key},
                timeout=(10, 15),
            )
        except RequestException as exc:
            logger.error(
                f"CANADA_VOICE_DELETE_FAILED - Exception during delete: {exc}"
            )
            return False

        if resp.status_code >= 400:
            logger.error(
                f"CANADA_VOICE_DELETE_FAILED - HTTP {resp.status_code}: {(resp.text or '')[:200]}"
            )
            return False

        # Check if deletion was successful
        try:
            delete_data = resp.json() or {}
            if delete_data.get("status") == "ok":
                logger.info(
                    f"CANADA_VOICE_DELETE_SUCCESS - Deleted voice {voice_name} ({voice_id[:10]}...)"
                )
                return True
        except ValueError:
            # Some APIs return text response
            body = (resp.text or "").lower()
            if "ok" in body or "success" in body:
                logger.info(
                    f"CANADA_VOICE_DELETE_SUCCESS - Deleted voice {voice_name} ({voice_id[:10]}...)"
                )
                return True

        logger.warning(
            f"CANADA_VOICE_DELETE_UNKNOWN - Unexpected delete response: {(resp.text or '')[:200]}"
        )
        return False

    except Exception as e:
        logger.error(f"CANADA_VOICE_DELETE_EXCEPTION - Exception while deleting voice: {e}")
        return False


def get_latest_history_item_id(api_key: str, voice_id: Optional[str], proxy_cfg: Optional[dict],
                               request_id: Optional[str] = None,
                               model_id: Optional[str] = None,
                               expected_text: Optional[str] = None,
                               expected_text_length: Optional[int] = None) -> Tuple[Optional[str], str]:
    """
    ULTRA-STRICT history matching using ONLY request_id.
    
    CRITICAL: 100% precise matching to eliminate cross-contamination:
    - ONLY request_id matching (100% success rate in logs)
    - NO text matching for selection (can cause errors in parallel processing)
    - Text length validation ONLY for verification (not for matching)
    - NO timestamp matching (unreliable in parallel processing)
    
    Args:
        request_id: Unique request identifier from Phase 1 response headers (REQUIRED)
        model_id: Model used for TTS generation (for filtering)
        expected_text: Expected text content (for validation only, NOT for matching)
        expected_text_length: Expected text length (for validation only, NOT for matching)
    """
    import logging
    import re
    logger = logging.getLogger('tts_service')
    
    base_url = "https://api.elevenlabs.io/v1/history"
    
    # Build params for requests
    params = {
        "page_size": 20,
        "sort_direction": "desc",
    }
    
    # CRITICAL: Always filter by voice_id + source for precision
    if voice_id:
        params["voice_id"] = voice_id
        params["source"] = "TTS"
        logger.info(f"HISTORY_SEARCH voice={voice_id}, source=TTS")
    
    if model_id:
        params["model_id"] = model_id
        logger.info(f"HISTORY_SEARCH + model={model_id}")
    
    # NO timestamp filtering - only request_id
    logger.info("HISTORY_SEARCH + NO timestamp filtering (using request_id ONLY)")

    # CRITICAL: Phase 2 search KHÔNG dùng proxy - giống tool_dgt.py
    # Chỉ Phase 1 (generate) dùng proxy, Phase 2 (search + download) dùng direct
    proxies = None  # KHÔNG dùng proxy cho Phase 2 search

    logger.info("HISTORY_SEARCH executing API call via requests (NO PROXY - Phase 2)...")
    try:
        resp = requests.get(
            base_url,
            headers={"xi-api-key": api_key},
            params=params,
            proxies=proxies,  # None - Phase 2 không dùng proxy
            timeout=(3, 15),
        )
    except RequestException as e:
        logger.error(f"HISTORY_SEARCH_EXCEPTION: {e}")
        return None, str(e)

    if resp.status_code != 200:
        logger.error(f"HISTORY_SEARCH_ERROR HTTP {resp.status_code}: {resp.text[:200]}")
        return None, f"HTTP_{resp.status_code}: {resp.text[:200]}"

    # Parse JSON response
    try:
        response_data = resp.json()

        # Debug: Show full response structure
        logger.info(f"HISTORY_SEARCH parsing response...")

        items = response_data.get('items', [])
        if not items:
            logger.warning(f"HISTORY_SEARCH_EMPTY - No history items found")
            return None, 'no_items_found'

        logger.info(f"HISTORY_SEARCH found {len(items)} items")

        # Show first few items for debugging
        for i, item in enumerate(items[:5]):
            item_voice_id = item.get('voice_id', '')[:16]  # Truncate for cleaner logs
            item_model_id = item.get('model_id', '')[:16]
            item_request_id = item.get('request_id', 'None')[:22]
            item_text = item.get('text', '')[:30]
            item_textlen = len(item.get('text', ''))
            logger.info(f"HISTORY_ITEM[{i}] voice={item_voice_id}, model={item_model_id}, request_id={item_request_id}, textlen={item_textlen}, text='{item_text}...'")

        # FIRST: Try exact request_id match (100% precision - ONLY safe method)
        if request_id:
            logger.info(f"HISTORY_MATCH_REQUEST_ID - Searching for request_id={request_id}")
            for item in items:
                if item.get('request_id') == request_id:
                    found_history_item_id = item.get('history_item_id')
                    item_text = item.get('text', '')
                    item_text_len = len(item_text)
                    
                    # VALIDATION: Verify text length matches (safety check, not for matching)
                    if expected_text_length and abs(item_text_len - expected_text_length) > 10:
                        logger.warning(f"HISTORY_MATCH_REQUEST_ID_TEXT_LEN_MISMATCH - request_id match but text len mismatch: expected={expected_text_length}, got={item_text_len}")
                        # Still return it if request_id matches (request_id is authoritative)
                    
                    # VALIDATION: Verify text content matches (safety check, not for matching)
                    if expected_text and expected_text.strip():
                        norm_expected = expected_text.strip().lower()
                        norm_item = item_text.strip().lower()
                        if norm_expected != norm_item:
                            logger.warning(f"HISTORY_MATCH_REQUEST_ID_TEXT_MISMATCH - request_id match but text content differs!")
                            logger.warning(f"  Expected: '{expected_text[:100]}...'")
                            logger.warning(f"  Got:      '{item_text[:100]}...'")
                            # Still return it if request_id matches (request_id is authoritative)
                    
                    logger.info(f"HISTORY_MATCH_REQUEST_ID_SUCCESS - Found exact match: history_item_id={found_history_item_id}, text_len={item_text_len}")
                    return found_history_item_id, 'request_id_match'

            logger.warning(f"HISTORY_MATCH_REQUEST_ID - No item with request_id={request_id}")

        # NO TEXT MATCHING FALLBACK - Too dangerous in parallel processing!
        # Text matching can cause cross-contamination when multiple threads run simultaneously
        # Example: paragraph 23 could get file from paragraph 15 if they have similar text
        # Disabled for safety - only request_id matching is allowed
        if False and expected_text and len(expected_text.strip()) > 0:
            logger.info(f"HISTORY_MATCH_NO_REQUEST_ID - Using text matching fallback")

            # Normalize text for matching
            # DISABLED: Text matching removed
            pass

            for item in items:
                item_text = item.get('text', '').strip()
                item_voice_id = item.get('voice_id', '')

                # Must match voice_id + text similarity
                if (item_voice_id == voice_id and
                    item_text.lower() == norm_gen_text):

                    found_history_item_id = item.get('history_item_id')
                    logger.info(f"HISTORY_MATCH_TEXT_FALLBACK \u2705 hist_id={found_history_item_id}, text='{item_text}...'")
                    return found_history_item_id, 'text_fallback_match'

            logger.warning(f"HISTORY_MATCH_TEXT_FALLBACK - No matching item found")

        # NO MATCH FOUND
        logger.error(f"HISTORY_MATCH_FAILED - No request_id or text match found")
        logger.error(f"CRITICAL - request_id={request_id}, voice_id={voice_id}")

        return None, 'no_match_found'

    except Exception as e:
        logger.error(f"HISTORY_SEARCH_EXCEPTION: {e}")
        return None, str(e)
    

def download_history_item_no_proxy(api_key: str, history_item_id: str, output_path: str, max_time: int = 300) -> Tuple[bool, str]:
    """
    Phase 2: Download MP3 qua DIRECT (mạng máy tính - không qua proxy)
    Giống tool_dgt.py: dùng endpoint /v1/history/{history_id}/audio với GET request
    
    QUAN TRỌNG: Phải dùng CÙNG api_key mà Phase 1 đã dùng để gen audio!
    
    NOTE: max_time=300 (5 phút) - không giới hạn chặt vì Phase 2 không tốn proxy,
    có thể download file lớn mà không lo timeout.
    """
    import logging
    logger = logging.getLogger('tts_service')

    # Dùng endpoint giống tool_dgt.py: /v1/history/{history_id}/audio
    url = f"https://api.elevenlabs.io/v1/history/{history_item_id}/audio"

    headers = {
        "xi-api-key": api_key,
        "Accept": "audio/mpeg",
    }

    # Retry loop - nếu 400 thì đợi 3s rồi thử lại (tối đa 15 lần, giống tool_dgt.py)
    max_retries = 15
    last_error = ""
    
    for attempt in range(max_retries):
        try:
            logger.info(f"CANADA_PHASE2 GET /v1/history/{history_item_id[:15]}.../audio" + (f" (retry {attempt+1})" if attempt > 0 else ""))
            
            # Dùng GET request với stream=True để download audio
            resp = requests.get(
                url,
                headers=headers,
                timeout=(5, max_time),
                stream=True,  # Dùng stream để download audio
                proxies=None,  # KHÔNG dùng proxy - Phase 2 dùng direct
            )
            
            info = f"HTTP_{resp.status_code}"

            if resp.status_code == 200:
                # Write audio to file
                try:
                    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
                except Exception:
                    pass

                tmp = output_path + ".part"
                recv_bytes = 0
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
                            recv_bytes += len(chunk)
                
                os.replace(tmp, output_path)
                
                ok = os.path.exists(output_path) and os.path.getsize(output_path) > 0
                if ok:
                    logger.info(f"CANADA_PHASE2 ✅ {recv_bytes/1024:.1f}KB → {os.path.basename(output_path)}")
                    return True, info
                else:
                    logger.error(f"CANADA_PHASE2 FAIL - File not created or empty")
                    return False, "File not created or empty"
            
            elif resp.status_code == 400 and attempt < max_retries - 1:
                # Nếu 400 và chưa hết retry → đợi 3s rồi thử lại (giống tool_dgt.py)
                logger.warning(f"CANADA_PHASE2 ⚠️ 400 - retry...")
                try:
                    resp.close()
                except:
                    pass
                time.sleep(3.0)
                continue
            else:
                # Lỗi khác hoặc hết retry
                try:
                    err_json = resp.json()
                    info = f"{info}: {err_json}"
                except Exception:
                    info = f"{info}: {resp.text[:200]}"
                logger.error(f"CANADA_PHASE2_HTTP_ERROR {info}")
                try:
                    resp.close()
                except:
                    pass
                if attempt == max_retries - 1:
                    return False, info
                time.sleep(2.0)
                continue
                
        except RequestException as e:
            last_error = str(e)
            logger.warning(f"CANADA_PHASE2_EXCEPTION attempt {attempt+1}/{max_retries}: {e}")
            if attempt == max_retries - 1:
                return False, f"REQUEST_EXCEPTION: {e}"
            time.sleep(2.0)
            continue

    return False, f"REQUEST_FAILED: {last_error}"


def canada_generate_then_download(api_key: str, voice_id: str, payload_json: str, proxy_cfg: dict,
                                  output_path: str, retries: int = 3) -> Tuple[bool, str]:
    """
    FIXED Canada 2-phase flow with request_id matching to prevent cross-contamination:
      Phase 1: Generate via Canada proxy → capture request_id from headers
      Phase 2: Find history item by request_id OR exact text matching → download via NO proxy
      
    Key fixes:
      - Use request_id for PRECISE mapping (eliminates cross-contamination)
      - Exact text content matching as fallback
      - NO MORE timestamp matching (unreliable in parallel processing)
      - Reduced wait time (3s → 1s) for faster processing
    """
    import logging
    import time
    import os
    logger = logging.getLogger('tts_service')

    # Track start time for performance measurement
    start_time = time.time()
    
    # Log proxy info safely
    proxy_info = "none"
    if proxy_cfg and proxy_cfg.get("host") and proxy_cfg.get("port"):
        try:
            port = int(proxy_cfg['port'])
            if 0 < port <= 65535:
                proxy_info = f"{proxy_cfg['host']}:{port}"
            else:
                proxy_info = f"{proxy_cfg['host']}:INVALID_PORT"
        except (ValueError, TypeError):
            proxy_info = f"{proxy_cfg['host']}:INVALID_PORT"

    logger.info(f"CANADA_2PHASE START - voice={voice_id}, proxy={proxy_info}")
    
    # Extract payload details BEFORE Phase 1 for precise matching
    try:
        payload = json.loads(payload_json)
        gen_text = (payload.get('text') or '')
        model_id = (payload.get('model_id') or 'eleven_turbo_v2_5')
        text_preview = gen_text[:80] if len(gen_text) > 80 else gen_text
        logger.info(f"CANADA_2PHASE PAYLOAD - text='{text_preview}', model={model_id}, len={len(gen_text)}")
    except Exception as e:
        logger.error(f"CANADA_2PHASE PAYLOAD_PARSE_ERROR: {e}")
        gen_text = ''
        model_id = ''
    
    # CRITICAL: Double-check credits BEFORE Phase 1 to prevent wasted API calls
    # STRICT: If credit check fails, we ABORT to prevent wasting credits
    # This is a safety check in case credits changed between STAGE2_PRE_CHECK and here
    credit_check_passed = False
    try:
        resp = requests.get(
            "https://api.elevenlabs.io/v1/user",
            headers={"xi-api-key": api_key},
            timeout=(5, 10),
        )
        if resp.status_code == 200:
            try:
                credit_data = resp.json() or {}
                subscription = credit_data.get('subscription', {}) or {}

                used = int(subscription.get('character_count') or 0)
                limit = int(subscription.get('character_limit') or 0)

                # Fallback for users on professional plans (per ElevenLabs API docs)
                if limit == 0:
                    pros_limit = int(subscription.get('professional_character_limit') or 0)
                    pros_used = int(subscription.get('professional_character_count') or 0)
                    if pros_limit:
                        limit = pros_limit
                        used = pros_used

                available_credits = max(0, limit - used) if limit else int(subscription.get('remaining_character_count') or 0)

                needed_chars = len(gen_text) if gen_text else 0
                buffer = max(50, int(needed_chars * 0.20)) if needed_chars > 0 else 50
                total_needed = needed_chars + buffer

                if available_credits < total_needed:
                    logger.error(
                        f"CANADA_2PHASE_CREDIT_CHECK - Key {api_key[:10]}... has {available_credits} credits "
                        f"but needs {total_needed} - ABORTING"
                    )
                    return False, f"INSUFFICIENT_CREDITS: {available_credits} < {total_needed}"
                else:
                    logger.info(
                        f"CANADA_2PHASE_CREDIT_CHECK ✅ - Key {api_key[:10]}... has {available_credits} credits "
                        f"(need {total_needed})"
                    )
                    credit_check_passed = True
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                logger.error(
                    f"CANADA_2PHASE_CREDIT_CHECK - Failed to parse credit response: {e} - ABORTING (strict mode)"
                )
                return False, f"CREDIT_CHECK_PARSE_ERROR: {e}"
        else:
            logger.error(f"CANADA_2PHASE_CREDIT_CHECK - Credit check failed HTTP {resp.status_code}: {resp.text[:200]} - ABORTING (strict mode)")
            return False, f"CREDIT_CHECK_FAILED: HTTP_{resp.status_code}"
    except RequestException as e:
        logger.error(f"CANADA_2PHASE_CREDIT_CHECK - RequestException during credit check: {e} - ABORTING (strict mode)")
        return False, f"CREDIT_CHECK_EXCEPTION: {e}"
    
    # CRITICAL: Only proceed if credit check passed
    if not credit_check_passed:
        logger.error(f"CANADA_2PHASE_CREDIT_CHECK - Credit check did not pass - ABORTING")
        return False, "CREDIT_CHECK_NOT_PASSED"
    
    # Phase 1: Generate via proxy and capture request_id
    logger.info(f"CANADA_PHASE1 START - textlen={len(gen_text)}")
    
    ok1, err1, request_id, history_item_id, is_quota_exceeded, is_voice_limit_reached = create_job_via_canada_proxy(voice_id, api_key, payload_json, proxy_cfg)
    
    logger.info(f"CANADA_PHASE1 RESULT - ok={ok1}, request_id={request_id}, history_item_id={history_item_id}, quota_exceeded={is_quota_exceeded}, voice_limit_reached={is_voice_limit_reached}, info='{err1[:200]}'")

    # CRITICAL: If quota_exceeded detected, return immediately with special flag
    if is_quota_exceeded:
        logger.error(f"CANADA_PHASE1_QUOTA_EXCEEDED - Key {api_key[:10]}... has insufficient credits!")
        return False, "QUOTA_EXCEEDED"
    
    # CRITICAL: If voice_limit_reached detected, try to delete a voice and retry
    if is_voice_limit_reached:
        logger.warning(f"CANADA_PHASE1_VOICE_LIMIT_REACHED - Key {api_key[:10]}... has reached voice limit, attempting to delete a voice...")
        voice_deleted = _try_delete_one_voice(api_key, logger)
        if voice_deleted:
            logger.info(f"CANADA_PHASE1_VOICE_DELETED - Successfully deleted a voice, retrying Phase 1...")
            # Retry Phase 1 after deleting voice
            ok1, err1, request_id, history_item_id, is_quota_exceeded, is_voice_limit_reached = create_job_via_canada_proxy(voice_id, api_key, payload_json, proxy_cfg)
            logger.info(f"CANADA_PHASE1_RETRY_RESULT - ok={ok1}, request_id={request_id}, history_item_id={history_item_id}, quota_exceeded={is_quota_exceeded}, voice_limit_reached={is_voice_limit_reached}, info='{err1[:200]}'")
            
            # If still quota_exceeded after retry, return immediately
            if is_quota_exceeded:
                logger.error(f"CANADA_PHASE1_QUOTA_EXCEEDED_AFTER_VOICE_DELETE - Key {api_key[:10]}... has insufficient credits after voice deletion!")
                return False, "QUOTA_EXCEEDED"
            
            # If still voice_limit_reached after retry, return error (shouldn't happen, but handle it)
            if is_voice_limit_reached:
                logger.error(f"CANADA_PHASE1_VOICE_LIMIT_STILL_REACHED - Key {api_key[:10]}... still has voice limit after deletion!")
                return False, "VOICE_LIMIT_REACHED"
        else:
            logger.error(f"CANADA_PHASE1_VOICE_DELETE_FAILED - Failed to delete a voice for key {api_key[:10]}...")
            return False, "VOICE_LIMIT_REACHED"

    if not ok1:
        logger.error(f"CANADA_PHASE1 FAILED - {err1[:300]}")
        return False, f"PHASE1_FAIL: {err1[:240]}"

    # OPTIMIZED: Wait time based on whether we have history_item_id
    # If we have history_item_id, we can download directly (no search needed) → shorter wait
    # If no history_item_id, we need to search history → longer wait for indexing
    if history_item_id:
        wait_time = 1.5  # 1.5 seconds - shorter wait since we don't need to search
        logger.info(f"CANADA_PHASE1 SUCCESS - waiting {wait_time}s (optimized: have history_item_id, no search needed)...")
    else:
        wait_time = 3.0  # 3 seconds - longer wait for history indexing when we need to search
        logger.info(f"CANADA_PHASE1 SUCCESS - waiting {wait_time}s for history indexing...")
    time.sleep(wait_time)

    # PRIORITY 1: If we have history_item_id from Phase 1, use it DIRECTLY (no need to search)
    # This is the FASTEST and MOST RELIABLE method - no API search needed!
    if history_item_id:
        logger.info(f"CANADA_DIRECT_DOWNLOAD - Using history_item_id from Phase 1: {history_item_id} (NO SEARCH NEEDED)")
        try:
            success2, err2 = download_history_item_no_proxy(api_key, history_item_id, output_path, max_time=300)
            if success2:
                # Validate file size and MP3 header
                file_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
                # MP3 files are typically 500-5000 bytes per character (depending on audio quality and model)
                # For short text (<100 chars), allow larger files due to MP3 encoding overhead
                text_len = len(gen_text) if gen_text else 0
                if text_len < 100:
                    # Short text: more lenient validation (MP3 overhead is higher percentage)
                    min_expected_size = max(5000, text_len * 300)  # Minimum 300 bytes/char for short text
                    max_expected_size = text_len * 5000 if text_len > 0 else float('inf')  # Maximum 5000 bytes/char for short text
                else:
                    # Normal text: more lenient validation to handle high-quality audio
                    min_expected_size = max(5000, text_len * 500)  # Minimum 500 bytes/char
                    max_expected_size = text_len * 5000 if text_len > 0 else float('inf')  # Maximum 5000 bytes/char (increased from 2000)

                # CRITICAL: Validate file size matches expected text length
                # Also validate MP3 header to ensure file is valid MP3
                is_valid_mp3 = False
                if file_size > 0:
                    try:
                        with open(output_path, 'rb') as f:
                            header = f.read(3)
                            # MP3 files start with ID3 tag (ID3) or MPEG frame sync (0xFF 0xFB/0xFA/0xF3/0xF2)
                            is_valid_mp3 = (header == b'ID3' or 
                                           (len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0))
                    except Exception:
                        pass

                # Accept file if:
                # 1. File size is within expected range, OR
                # 2. File size is larger but still reasonable (< 5000 bytes/char) AND file is valid MP3
                bytes_per_char = file_size / text_len if text_len > 0 else float('inf')
                if (file_size >= min_expected_size and file_size <= max_expected_size) or \
                   (file_size > max_expected_size and bytes_per_char < 5000 and is_valid_mp3):
                    logger.info(f"CANADA_DIRECT_DOWNLOAD SUCCESS - size={file_size} bytes ({bytes_per_char:.1f} bytes/char), MP3 valid={is_valid_mp3}, generated in {time.time()-start_time:.1f}s")
                    return True, f"direct_download_success: {file_size} bytes"
                else:
                    logger.warning(f"CANADA_DIRECT_DOWNLOAD - File size mismatch: {file_size} bytes ({bytes_per_char:.1f} bytes/char, expected {min_expected_size}-{max_expected_size}), MP3 valid={is_valid_mp3}")
                    logger.warning(f"  This may indicate wrong file was downloaded! Text len={text_len}")
                    # Fall through to Phase 2 search if direct download fails validation
            else:
                logger.warning(f"CANADA_DIRECT_DOWNLOAD failed: {err2[:100]}, falling back to Phase 2 search...")
        except Exception as e:
            logger.warning(f"CANADA_DIRECT_DOWNLOAD error: {e}, falling back to Phase 2 search...")
    else:
        logger.info(f"CANADA_DIRECT_DOWNLOAD - No history_item_id from Phase 1, will use Phase 2 search with request_id={request_id}")

    # Phase 2: Find history item using request_id matching with fallback
    logger.info(f"CANADA_PHASE2 START - Using request_id matching with fallback")

    # OPTIMIZED: Reduced cycles for faster failure detection
    max_cycles = 2
    
    for cycle in range(1, max_cycles + 1):
        logger.info(f"CANADA_2PHASE_CYCLE {cycle}/{max_cycles}")
        
        # PRIORITY 1: If we have history_item_id (from initial Phase 1 or previous cycle), use it DIRECTLY
        # This is the FASTEST and MOST RELIABLE method - no API search needed!
        if history_item_id:
            logger.info(f"CANADA_CYCLE_{cycle}_DIRECT_DOWNLOAD - Using history_item_id: {history_item_id} (NO SEARCH NEEDED)")
            try:
                success2, err2 = download_history_item_no_proxy(api_key, history_item_id, output_path, max_time=300)
                if success2:
                    # Validate file size and MP3 header
                    file_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
                    # MP3 files are typically 500-5000 bytes per character (depending on audio quality and model)
                    # For short text (<100 chars), allow larger files due to MP3 encoding overhead
                    text_len = len(gen_text) if gen_text else 0
                    if text_len < 100:
                        # Short text: more lenient validation (MP3 overhead is higher percentage)
                        min_expected_size = max(5000, text_len * 300)  # Minimum 300 bytes/char for short text
                        max_expected_size = text_len * 5000 if text_len > 0 else float('inf')  # Maximum 5000 bytes/char
                    else:
                        # Normal text: more lenient validation to handle high-quality audio
                        min_expected_size = max(5000, text_len * 500)  # Minimum 500 bytes/char
                        max_expected_size = text_len * 5000 if text_len > 0 else float('inf')  # Maximum 5000 bytes/char (increased from 2000)
                    
                    # Validate MP3 header
                    is_valid_mp3 = False
                    if file_size > 0:
                        try:
                            with open(output_path, 'rb') as f:
                                header = f.read(3)
                                is_valid_mp3 = (header == b'ID3' or 
                                               (len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0))
                        except Exception:
                            pass
                    
                    bytes_per_char = file_size / text_len if text_len > 0 else float('inf')
                    if (file_size >= min_expected_size and file_size <= max_expected_size) or \
                       (file_size > max_expected_size and bytes_per_char < 5000 and is_valid_mp3):
                        logger.info(f"CANADA_CYCLE_{cycle}_DIRECT_DOWNLOAD SUCCESS - size={file_size} bytes ({bytes_per_char:.1f} bytes/char), MP3 valid={is_valid_mp3}")
                        return True, f"direct_download_success: {file_size} bytes"
                    else:
                        logger.warning(f"CANADA_CYCLE_{cycle}_DIRECT_DOWNLOAD - File size mismatch: {file_size} bytes ({bytes_per_char:.1f} bytes/char, expected {min_expected_size}-{max_expected_size}), MP3 valid={is_valid_mp3}")
                        # File size validation failed, but continue to try Phase 2 search as fallback
                else:
                    logger.warning(f"CANADA_CYCLE_{cycle}_DIRECT_DOWNLOAD failed: {err2[:100]}, falling back to Phase 2 search...")
            except Exception as e:
                logger.warning(f"CANADA_CYCLE_{cycle}_DIRECT_DOWNLOAD error: {e}, falling back to Phase 2 search...")
        
        # If no request_id, retry Phase 1
        if not request_id:
            logger.warning(f"CANADA_CYCLE_{cycle} - No request_id, retrying Phase 1...")
            ok1_retry, err1_retry, request_id_retry, history_item_id_retry, is_quota_exceeded_retry, is_voice_limit_reached_retry = create_job_via_canada_proxy(voice_id, api_key, payload_json, proxy_cfg)
            
            # CRITICAL: If quota_exceeded in retry, abort immediately
            if is_quota_exceeded_retry:
                logger.error(f"CANADA_CYCLE_{cycle}_PHASE1_QUOTA_EXCEEDED - Key {api_key[:10]}... has insufficient credits!")
                return False, "QUOTA_EXCEEDED"
            
            # CRITICAL: If voice_limit_reached in retry, try to delete a voice and retry again
            if is_voice_limit_reached_retry:
                logger.warning(f"CANADA_CYCLE_{cycle}_VOICE_LIMIT_REACHED - Key {api_key[:10]}... has reached voice limit, attempting to delete a voice...")
                voice_deleted = _try_delete_one_voice(api_key, logger)
                if voice_deleted:
                    logger.info(f"CANADA_CYCLE_{cycle}_VOICE_DELETED - Successfully deleted a voice, retrying Phase 1 again...")
                    # Retry Phase 1 after deleting voice
                    ok1_retry, err1_retry, request_id_retry, history_item_id_retry, is_quota_exceeded_retry, is_voice_limit_reached_retry = create_job_via_canada_proxy(voice_id, api_key, payload_json, proxy_cfg)
                    logger.info(f"CANADA_CYCLE_{cycle}_PHASE1_RETRY_RESULT - ok={ok1_retry}, request_id={request_id_retry}, history_item_id={history_item_id_retry}, quota_exceeded={is_quota_exceeded_retry}, voice_limit_reached={is_voice_limit_reached_retry}")
                    
                    # If still quota_exceeded after retry, abort immediately
                    if is_quota_exceeded_retry:
                        logger.error(f"CANADA_CYCLE_{cycle}_PHASE1_QUOTA_EXCEEDED - Key {api_key[:10]}... has insufficient credits after voice deletion!")
                        return False, "QUOTA_EXCEEDED"
                    
                    # If still voice_limit_reached after retry, return error (shouldn't happen, but handle it)
                    if is_voice_limit_reached_retry:
                        logger.error(f"CANADA_CYCLE_{cycle}_VOICE_LIMIT_STILL_REACHED - Key {api_key[:10]}... still has voice limit after deletion!")
                        return False, "VOICE_LIMIT_REACHED"
                else:
                    logger.error(f"CANADA_CYCLE_{cycle}_VOICE_DELETE_FAILED - Failed to delete a voice for key {api_key[:10]}...")
                    return False, "VOICE_LIMIT_REACHED"
            
            if ok1_retry and request_id_retry:
                logger.info(f"CANADA_CYCLE_{cycle}_PHASE1 SUCCESS - Captured request_id: {request_id_retry}")
                request_id = request_id_retry
                if history_item_id_retry:
                    history_item_id = history_item_id_retry
                    logger.info(f"CANADA_CYCLE_{cycle}_PHASE1 SUCCESS - Also captured history_item_id: {history_item_id}")
                    # CRITICAL: If we got history_item_id from retry, use it DIRECTLY (no need to search)
                    logger.info(f"CANADA_CYCLE_{cycle}_DIRECT_DOWNLOAD - Using history_item_id from retry: {history_item_id} (NO SEARCH NEEDED)")
                    try:
                        success2, err2 = download_history_item_no_proxy(api_key, history_item_id, output_path, max_time=300)
                        if success2:
                            file_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
                            # For short text (<100 chars), allow larger files due to MP3 encoding overhead
                            text_len = len(gen_text) if gen_text else 0
                            if text_len < 100:
                                min_expected_size = max(5000, text_len * 300)
                                max_expected_size = text_len * 5000 if text_len > 0 else float('inf')  # Increased from 3000
                            else:
                                min_expected_size = max(5000, text_len * 500)
                                max_expected_size = text_len * 5000 if text_len > 0 else float('inf')  # Increased from 2000
                            
                            # Validate MP3 header
                            is_valid_mp3 = False
                            if file_size > 0:
                                try:
                                    with open(output_path, 'rb') as f:
                                        header = f.read(3)
                                        is_valid_mp3 = (header == b'ID3' or 
                                                       (len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0))
                                except Exception:
                                    pass
                            
                            bytes_per_char = file_size / text_len if text_len > 0 else float('inf')
                            if (file_size >= min_expected_size and file_size <= max_expected_size) or \
                               (file_size > max_expected_size and bytes_per_char < 5000 and is_valid_mp3):
                                logger.info(f"CANADA_CYCLE_{cycle}_DIRECT_DOWNLOAD SUCCESS - size={file_size} bytes ({bytes_per_char:.1f} bytes/char), MP3 valid={is_valid_mp3}")
                                return True, f"direct_download_success: {file_size} bytes"
                            else:
                                logger.warning(f"CANADA_CYCLE_{cycle}_DIRECT_DOWNLOAD - File size mismatch: {file_size} bytes ({bytes_per_char:.1f} bytes/char, expected {min_expected_size}-{max_expected_size}), MP3 valid={is_valid_mp3}")
                        else:
                            logger.warning(f"CANADA_CYCLE_{cycle}_DIRECT_DOWNLOAD failed: {err2[:100]}, falling back to Phase 2 search...")
                    except Exception as e:
                        logger.warning(f"CANADA_CYCLE_{cycle}_DIRECT_DOWNLOAD error: {e}, falling back to Phase 2 search...")
                else:
                    # CRITICAL: Wait for history indexing only if no history_item_id
                    # Shorter wait since we'll need to search anyway
                    wait_time = 2.0
                    logger.info(f"CANADA_CYCLE_{cycle}_PHASE1 - waiting {wait_time}s for history indexing...")
                    time.sleep(wait_time)
            else:
                logger.error(f"CANADA_CYCLE_{cycle}_PHASE1 FAILED - {err1_retry[:200]}")
                continue  # Try next cycle
        
        # Try Phase 2 with request_id (only if no history_item_id or direct download failed)
        if request_id and not history_item_id:
            logger.info(f"CANADA_CYCLE_{cycle}_PHASE2 - Searching with request_id={request_id}")
            
            hist_id, herr = get_latest_history_item_id(
                api_key, voice_id, proxy_cfg,
                request_id=request_id,
                model_id=model_id,
                expected_text=gen_text,
                expected_text_length=len(gen_text) if gen_text else None
            )
            
            if hist_id:
                logger.info(f"CANADA_CYCLE_{cycle}_PHASE2 - found hist_id={hist_id}, downloading...")
                ok2, err2 = download_history_item_no_proxy(api_key, hist_id, output_path, max_time=300)
                
                if ok2:
                    file_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
                    
                    # CRITICAL: Validate file size matches expected text length
                    # For short text (<100 chars), allow larger files due to MP3 encoding overhead
                    text_len = len(gen_text) if gen_text else 0
                    if text_len < 100:
                        min_expected_size = max(5000, text_len * 300) if text_len > 0 else 5000
                        max_expected_size = text_len * 5000 if text_len > 0 else float('inf')  # Increased from 3000
                    else:
                        min_expected_size = max(5000, text_len * 500) if text_len > 0 else 5000
                        max_expected_size = text_len * 5000 if text_len > 0 else float('inf')  # Increased from 2000
                    
                    # Validate MP3 header
                    is_valid_mp3 = False
                    if file_size > 0:
                        try:
                            with open(output_path, 'rb') as f:
                                header = f.read(3)
                                is_valid_mp3 = (header == b'ID3' or 
                                               (len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0))
                        except Exception:
                            pass
                    
                    bytes_per_char = file_size / text_len if text_len > 0 else float('inf')
                    if (file_size >= min_expected_size and file_size <= max_expected_size) or \
                       (file_size > max_expected_size and bytes_per_char < 5000 and is_valid_mp3):
                        logger.info(f"CANADA_2PHASE SUCCESS - cycle {cycle}/{max_cycles}, file_size={file_size} bytes ({bytes_per_char:.1f} bytes/char, expected {min_expected_size}-{max_expected_size}), MP3 valid={is_valid_mp3}")
                        return True, ''
                    else:
                        logger.error(f"CANADA_CYCLE_{cycle}_PHASE2 - FILE SIZE MISMATCH - size={file_size} bytes ({bytes_per_char:.1f} bytes/char, expected {min_expected_size}-{max_expected_size}), MP3 valid={is_valid_mp3}")
                        logger.error(f"  This may indicate wrong file was downloaded! Text len={text_len}")
                        try:
                            os.unlink(output_path)
                        except Exception:
                            pass
                else:
                    logger.error(f"CANADA_CYCLE_{cycle}_PHASE2 - download failed: {err2[:200]}")
            else:
                logger.warning(f"CANADA_CYCLE_{cycle}_PHASE2 - no request_id match: {herr[:100]}")
                # NO TEXT MATCHING FALLBACK - Too dangerous in parallel processing!
                # Text matching can cause cross-contamination (e.g., paragraph 23 gets file from paragraph 15)

        # CRITICAL: Only reset request_id if we don't have history_item_id
        # If we have history_item_id, we should have already tried direct download
        # If direct download failed, we should NOT reset history_item_id - it's still valid!
        # Only reset request_id to force Phase 1 retry if we don't have history_item_id
        if not history_item_id:
            request_id = None  # Reset request_id for next cycle to force Phase 1 retry
        # If we have history_item_id, keep it for next cycle (in case direct download failed due to network issue)
        
        # Wait before next cycle (unless last cycle)
        if cycle < max_cycles:
            # logger.info(f"CANADA_CYCLE_{cycle} - waiting 1s before next cycle...")
            # time.sleep(1)  # ELIMINATED: No wait for maximum speed
            pass  # No-op for speed

    # All cycles exhausted
    logger.error(f"CANADA_2PHASE FAILED - exhausted {max_cycles} full cycles")
    return False, f"ALL_CYCLES_EXHAUSTED: No valid request_id match found"


