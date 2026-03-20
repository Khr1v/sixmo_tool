#!/usr/bin/env python3
"""Complete sixmo.ru multi-step challenge flow via API using Playwright."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import random
import sys
import time
from typing import Any, Dict, Iterable, Mapping, Optional

from playwright.sync_api import APIRequestContext, Playwright, sync_playwright


STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {
  get: () => undefined
});

Object.defineProperty(navigator, 'languages', {
  get: () => ['ru-RU', 'ru']
});

Object.defineProperty(navigator, 'plugins', {
  get: () => [1, 2, 3, 4, 5]
});
"""


def normalize_text(value: str) -> str:
    return " ".join(value.strip().lower().replace("ё", "е").split())


def now_ms() -> int:
    return int(time.time() * 1000)


class SixmoFlowError(RuntimeError):
    pass


class SixmoApiRunner:
    def __init__(
        self,
        base_url: str,
        request_timeout_ms: int,
        poll_timeout_s: int,
        max_retries: int,
        verbose: bool,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.request_timeout_ms = request_timeout_ms
        self.poll_timeout_s = poll_timeout_s
        self.max_retries = max_retries
        self.verbose = verbose
        self._request: Optional[APIRequestContext] = None

    def run(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        with sync_playwright() as playwright:
            bootstrap_mode = str(payload.get("bootstrap_mode") or "ui").strip().lower()
            browser = None
            browser_context = None
            start_override: Optional[Dict[str, Any]] = None
            request_owned = False

            if bootstrap_mode == "ui":
                browser_channel = str(payload.get("browser_channel") or "chrome").strip()
                headless = bool(payload.get("headless", False))
                user_agent = str(payload.get("user_agent") or chrome_user_agent())
                timezone_id = str(payload.get("timezone_id") or "Europe/Moscow")
                min_start_delay_ms = int(payload.get("min_start_delay_ms") or 2500)
                max_start_delay_ms = int(payload.get("max_start_delay_ms") or 6500)

                try:
                    browser = playwright.chromium.launch(
                        channel=browser_channel if browser_channel else None,
                        headless=headless,
                    )
                except Exception as launch_err:  # noqa: BLE001
                    if browser_channel:
                        self._log(
                            f"Chrome channel launch failed ({launch_err}); fallback to bundled chromium"
                        )
                        browser = playwright.chromium.launch(headless=headless)
                    else:
                        raise

                browser_context = browser.new_context(
                    base_url=self.base_url,
                    locale="ru-RU",
                    timezone_id=timezone_id,
                    viewport={"width": 1280, "height": 800},
                    user_agent=user_agent,
                    extra_http_headers=default_api_headers(user_agent=user_agent),
                )
                browser_context.add_init_script(STEALTH_INIT_SCRIPT)
                self._request = browser_context.request
                start_override = self._start_via_ui(
                    browser_context,
                    min_start_delay_ms=min_start_delay_ms,
                    max_start_delay_ms=max_start_delay_ms,
                )
            else:
                self._request = playwright.request.new_context(
                    base_url=self.base_url,
                    extra_http_headers=default_api_headers(user_agent=chrome_user_agent()),
                )
                request_owned = True

            try:
                return self._run_inner(playwright, payload, start_override=start_override)
            finally:
                if self._request is not None and request_owned:
                    self._request.dispose()
                self._request = None
                if browser_context is not None:
                    browser_context.close()
                if browser is not None:
                    browser.close()

    def _run_inner(
        self,
        _playwright: Playwright,
        payload: Mapping[str, Any],
        start_override: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        if start_override is None:
            start_payload = payload.get("start_payload") or {"fingerprint": default_fingerprint()}
            start = self._request_json("POST", "/api/start.php", data=start_payload)
        else:
            start = dict(start_override)
        if not start.get("ok"):
            raise SixmoFlowError(f"start.php returned non-ok payload: {start}")

        flow_id = must_str(start.get("flowId"), "Missing flowId in start.php response")
        flow_key = must_str(start.get("flowKey"), "Missing flowKey in start.php response")
        csrf = must_str(start.get("csrfToken"), "Missing csrfToken in start.php response")
        headers = {"x-flow-key": flow_key, "x-csrf-token": csrf}
        submitted_answers: list[Dict[str, Any]] = []

        self._log(f"Flow started: flow_id={flow_id}")

        step1 = self._wait_step_ready(flow_id, 1, headers)
        submit1_pack = self._submit_step(
            flow_id=flow_id,
            step=1,
            step_data=step1,
            payload=payload,
            headers=headers,
        )
        submit1 = submit1_pack["response"]
        submitted_answers.extend(submit1_pack["submittedFields"])
        if not submit1.get("ok"):
            raise SixmoFlowError(f"Step 1 submit failed: {submit1}")

        next_step = submit1.get("nextStep")
        if submit1.get("next") != "step" or next_step != 2:
            raise SixmoFlowError(f"Unexpected step 1 submit response: {submit1}")

        step2 = self._wait_step_ready(flow_id, 2, headers)
        submit2_pack = self._submit_step(
            flow_id=flow_id,
            step=2,
            step_data=step2,
            payload=payload,
            headers=headers,
        )
        submit2 = submit2_pack["response"]
        submitted_answers.extend(submit2_pack["submittedFields"])
        if not submit2.get("ok"):
            raise SixmoFlowError(f"Step 2 submit failed: {submit2}")
        if submit2.get("next") != "result":
            raise SixmoFlowError(f"Unexpected step 2 submit response: {submit2}")

        result = self._request_json("GET", f"/api/result.php?flow_id={flow_id}", headers=headers)
        if not result.get("ok"):
            raise SixmoFlowError(f"result.php returned non-ok payload: {result}")

        final_identifier = must_str(
            result.get("finalIdentifier"), "Missing finalIdentifier in result.php response"
        )
        completed_at = must_str(result.get("completedAt"), "Missing completedAt in result.php response")
        self._log(f"Форма пройдена, идентификационный номер: {final_identifier}")

        return {
            "ok": True,
            "mode": "api",
            "flowId": flow_id,
            "finalIdentifier": final_identifier,
            "completedAt": completed_at,
            "submittedAnswers": submitted_answers,
            "raw": {
                "start": start,
                "submitStep1": submit1,
                "submitStep2": submit2,
                "result": result,
            },
        }

    def _start_via_ui(
        self,
        browser_context: Any,
        min_start_delay_ms: int,
        max_start_delay_ms: int,
    ) -> Dict[str, Any]:
        attempts = 2
        last_error: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            page = browser_context.new_page()
            try:
                page.goto("/", wait_until="domcontentloaded", timeout=self.request_timeout_ms)
                page.wait_for_timeout(random.randint(500, 1200))
                button = page.get_by_role("button", name="Начать задание").first
                button.wait_for(state="visible", timeout=self.request_timeout_ms)

                env = page.evaluate(
                    "() => ({"
                    "webdriver: navigator.webdriver,"
                    "languages: Array.from(navigator.languages || []),"
                    "pluginsLength: navigator.plugins ? navigator.plugins.length : 0,"
                    "userAgent: navigator.userAgent,"
                    "hasPlaywrightBinding: !!window.__playwright__"
                    "})"
                )
                self._log(f"Bootstrap env (attempt {attempt}): {env}")

                dwell = random.randint(max(500, min_start_delay_ms), max(min_start_delay_ms, max_start_delay_ms))
                page.wait_for_timeout(dwell)

                # Add light human-like interaction before first click.
                width = page.viewport_size["width"] if page.viewport_size else 1280
                height = page.viewport_size["height"] if page.viewport_size else 800
                page.mouse.move(width * 0.42, height * 0.28)
                page.mouse.move(width * 0.61, height * 0.36)
                page.mouse.wheel(0, random.randint(60, 180))
                page.mouse.wheel(0, random.randint(-120, -40))
                page.wait_for_timeout(random.randint(200, 650))

                with page.expect_response(
                    lambda resp: resp.request.method.upper() == "POST" and "/api/start.php" in resp.url,
                    timeout=self.request_timeout_ms,
                ) as start_info:
                    clicked = False
                    selectors = [
                        page.get_by_role("button", name="Начать задание"),
                        page.locator("button.primary-button:has-text('Начать задание')"),
                        page.locator("button:has-text('Начать задание')"),
                    ]
                    for selector in selectors:
                        try:
                            selector.first.click(timeout=2500, delay=random.randint(20, 90))
                            clicked = True
                            break
                        except Exception:  # noqa: BLE001
                            continue

                    if not clicked:
                        raise SixmoFlowError("Cannot click start button 'Начать задание'")

                start_resp = start_info.value
                if start_resp.status >= 400:
                    body = start_resp.text()[:400]
                    raise SixmoFlowError(f"start.php HTTP {start_resp.status} after UI bootstrap: {body}")
                data = start_resp.json()
                if not isinstance(data, dict):
                    raise SixmoFlowError(f"Invalid start payload from UI bootstrap: {data!r}")
                self._log("Start bootstrap completed via UI")
                return data
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                self._log(f"UI bootstrap attempt {attempt} failed: {exc}")
                if attempt < attempts:
                    page.wait_for_timeout(random.randint(400, 1000))
            finally:
                page.close()

        assert last_error is not None
        raise SixmoFlowError(f"UI bootstrap failed after {attempts} attempts: {last_error}")

    def _wait_step_ready(self, flow_id: str, step: int, headers: Mapping[str, str]) -> Dict[str, Any]:
        start_ts = time.time()
        attempts = 0
        while True:
            attempts += 1
            response = self._request_json(
                "GET",
                f"/api/step.php?flow_id={flow_id}&step={step}",
                headers=headers,
            )
            if not response.get("ok"):
                raise SixmoFlowError(f"step.php returned non-ok payload for step={step}: {response}")

            status = response.get("status")
            if status == "ready":
                step_data = response.get("stepData")
                if not isinstance(step_data, dict):
                    raise SixmoFlowError(f"stepData is missing or invalid for step={step}: {response}")
                self._log(f"Step {step} is ready after {attempts} poll(s)")
                return step_data

            if status != "pending":
                raise SixmoFlowError(f"Unexpected step status for step={step}: {response}")

            elapsed = time.time() - start_ts
            if elapsed > self.poll_timeout_s:
                raise SixmoFlowError(
                    f"Timeout waiting for step={step} to become ready after {self.poll_timeout_s} seconds"
                )

            retry_ms = int(response.get("retryAfterMs") or 1000)
            sleep_s = max(0.2, min(retry_ms / 1000.0, 8.0))
            sleep_s += random.uniform(0.05, 0.25)
            self._log(f"Step {step} pending; sleeping {sleep_s:.2f}s (retryAfterMs={retry_ms})")
            time.sleep(sleep_s)

    def _submit_step(
        self,
        flow_id: str,
        step: int,
        step_data: Mapping[str, Any],
        payload: Mapping[str, Any],
        headers: Mapping[str, str],
    ) -> Dict[str, Any]:
        answers = merged_answers(payload, step)
        fields = as_list(step_data.get("fields"), "stepData.fields must be a list")
        step_token = must_str(step_data.get("stepToken"), "stepData.stepToken is missing")
        submitted_fields: list[Dict[str, Any]] = []

        multipart: Dict[str, Any] = {
            "flow_id": flow_id,
            "step": str(step),
            "step_token": step_token,
        }

        step_started_at = time.time()
        for field in fields:
            if not isinstance(field, dict):
                raise SixmoFlowError(f"Invalid field descriptor in step={step}: {field!r}")

            field_name = must_str(field.get("name"), f"Field has no name in step={step}")
            field_type = must_str(field.get("type"), f"Field {field_name} has no type in step={step}")
            question = str(field.get("label") or field_name)

            if field_type == "file":
                file_path = resolve_file_path(field, payload, answers)
                multipart[field_name] = build_file_part(file_path)
                answer_display = f"[file] {os.path.basename(file_path)}"
                submitted_fields.append(
                    {
                        "step": step,
                        "fieldName": field_name,
                        "fieldType": field_type,
                        "question": question,
                        "answer": answer_display,
                        "answerValue": os.path.basename(file_path),
                    }
                )
                self._log_step_answer(step, question, answer_display)
                continue

            answer = resolve_answer(field, answers)
            if answer is None:
                label = str(field.get("label") or field_name)
                raise SixmoFlowError(
                    f"Missing answer for step={step}, field={field_name}, label={label!r}"
                )

            if field_type == "select":
                option_value = resolve_select_value(field, str(answer))
                multipart[field_name] = option_value
                selected_label = select_label_by_value(field, option_value)
                answer_display = f"{selected_label} (value={option_value})"
                submitted_fields.append(
                    {
                        "step": step,
                        "fieldName": field_name,
                        "fieldType": field_type,
                        "question": question,
                        "answer": answer_display,
                        "answerValue": option_value,
                    }
                )
                self._log_step_answer(
                    step,
                    question,
                    answer_display,
                )
            else:
                text_answer = str(answer)
                multipart[field_name] = text_answer
                submitted_fields.append(
                    {
                        "step": step,
                        "fieldName": field_name,
                        "fieldType": field_type,
                        "question": question,
                        "answer": text_answer,
                        "answerValue": text_answer,
                    }
                )
                self._log_step_answer(step, question, text_answer)

        telemetry = payload.get("telemetry") or build_telemetry(fields, step_started_at)
        multipart["telemetry"] = json.dumps(telemetry, ensure_ascii=False)

        response = self._request_json(
            "POST",
            "/api/submit.php",
            headers=headers,
            multipart=multipart,
        )
        self._log(f"Submitted step {step}: {response}")
        return {
            "response": response,
            "submittedFields": submitted_fields,
        }

    def _request_json(
        self,
        method: str,
        path: str,
        headers: Optional[Mapping[str, str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        if self._request is None:
            raise SixmoFlowError("Request context is not initialized")

        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self._request.fetch(
                    path,
                    method=method,
                    headers=headers,
                    timeout=self.request_timeout_ms,
                    **kwargs,
                )
                status = response.status
                if status >= 500:
                    raise SixmoFlowError(f"HTTP {status} for {method} {path}: {response.text()[:400]}")
                if status >= 400:
                    raise SixmoFlowError(f"HTTP {status} for {method} {path}: {response.text()[:400]}")
                data = response.json()
                if not isinstance(data, dict):
                    raise SixmoFlowError(f"Expected JSON object in {method} {path}, got: {data!r}")
                return data
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt >= self.max_retries:
                    break
                backoff = 0.35 * attempt
                self._log(f"Request failed ({method} {path}) attempt={attempt}: {exc}. Backoff {backoff:.2f}s")
                time.sleep(backoff)

        assert last_error is not None
        raise SixmoFlowError(f"Request failed after {self.max_retries} attempts: {method} {path}") from last_error

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[sixmo] {message}", file=sys.stderr)

    def _log_step_answer(self, step: int, question: str, answer: str) -> None:
        safe_question = " ".join(question.split())
        safe_answer = " ".join(answer.split())
        self._log(f"Шаг {step} | Вопрос: {safe_question} | Ответ: {safe_answer}")


def resolve_select_value(field: Mapping[str, Any], answer: str) -> str:
    options = as_list(field.get("options"), "select field options must be a list")

    answer_norm = normalize_text(answer)
    for option in options:
        if not isinstance(option, dict):
            continue
        value = str(option.get("value", ""))
        label = str(option.get("label", ""))
        if answer == value:
            return value
        if answer == label:
            return value
        if answer_norm == normalize_text(label):
            return value

    pretty_options = [f"{o.get('label')} ({o.get('value')})" for o in options if isinstance(o, dict)]
    field_name = field.get("name")
    raise SixmoFlowError(
        f"Cannot match select answer {answer!r} for field={field_name}. Options: {pretty_options}"
    )


def select_label_by_value(field: Mapping[str, Any], value: str) -> str:
    options = field.get("options")
    if not isinstance(options, list):
        return value
    for option in options:
        if not isinstance(option, dict):
            continue
        if str(option.get("value", "")) == value:
            label = str(option.get("label", "")).strip()
            return label or value
    return value


def resolve_answer(field: Mapping[str, Any], answers: Mapping[str, Any]) -> Optional[Any]:
    field_name = str(field.get("name") or "")
    field_label = str(field.get("label") or "")

    if field_name in answers:
        return answers[field_name]
    if field_label in answers:
        return answers[field_label]

    normalized = {normalize_text(str(key)): value for key, value in answers.items()}

    if normalize_text(field_name) in normalized:
        return normalized[normalize_text(field_name)]
    if normalize_text(field_label) in normalized:
        return normalized[normalize_text(field_label)]

    return None


def resolve_file_path(
    field: Mapping[str, Any],
    payload: Mapping[str, Any],
    answers: Mapping[str, Any],
) -> str:
    answer_path = resolve_answer(field, answers)
    if isinstance(answer_path, str) and answer_path.strip():
        path = answer_path.strip()
    else:
        path = str(payload.get("file_path") or "").strip()

    if not path:
        raise SixmoFlowError("file_path is required for file upload field")
    if not os.path.isfile(path):
        raise SixmoFlowError(f"File not found: {path}")
    if os.path.getsize(path) > 50 * 1024:
        raise SixmoFlowError(f"File must be <= 50KB: {path}")
    return path


def build_file_part(path: str) -> Dict[str, Any]:
    mime, _ = mimetypes.guess_type(path)
    if not mime:
        mime = "application/octet-stream"
    with open(path, "rb") as file_obj:
        data = file_obj.read()
    return {"name": os.path.basename(path), "mimeType": mime, "buffer": data}


def build_telemetry(fields: Iterable[Mapping[str, Any]], step_started_at: float) -> Dict[str, Any]:
    field_descriptors = [field for field in fields if isinstance(field, Mapping)]
    field_names = [str(field.get("name", "")) for field in field_descriptors if str(field.get("name", ""))]
    non_file_names = [name for name in field_names if name]
    text_field_names = [
        str(field.get("name", ""))
        for field in field_descriptors
        if str(field.get("type", "")).lower() in {"text", "textarea"}
    ]
    focus_sequence = [name for name in non_file_names]
    if not focus_sequence:
        focus_sequence = text_field_names[:]

    # Generate realistic typing intervals similar to manual flow traces.
    key_intervals: list[int] = []
    for _ in range(random.randint(8, 22)):
        base = random.randint(95, 420)
        if random.random() < 0.12:
            base += random.randint(350, 1800)
        key_intervals.append(base)

    if key_intervals:
        avg = sum(key_intervals) / len(key_intervals)
        variance = sum((value - avg) ** 2 for value in key_intervals) / len(key_intervals)
    else:
        avg = 0.0
        variance = 0.0

    observed_elapsed = int((time.time() - step_started_at) * 1000)
    dwell_floor = random.randint(12000, 26000)
    dwell_ms = max(dwell_floor, observed_elapsed)

    return {
        "dwellMs": dwell_ms,
        "keyIntervals": key_intervals,
        "averageKeyInterval": avg,
        "intervalVariance": variance,
        "mouseMoves": random.randint(160, 420),
        "scrollCount": random.randint(8, 34),
        "clicks": max(4, len(field_names) + random.randint(2, 5)),
        "displayedFields": non_file_names,
        "fieldSequence": [name for name in text_field_names if name],
        "focusSequence": focus_sequence,
        "userAgent": chrome_user_agent(),
        "webdriver": False,
        "hasPlaywrightBinding": False,
    }


def merged_answers(payload: Mapping[str, Any], step: int) -> Dict[str, Any]:
    answers = payload.get("answers") or {}
    if not isinstance(answers, dict):
        raise SixmoFlowError("'answers' must be an object")

    step_answers_map = payload.get("step_answers") or {}
    if step_answers_map and not isinstance(step_answers_map, dict):
        raise SixmoFlowError("'step_answers' must be an object")

    step_answers = {}
    if isinstance(step_answers_map, dict):
        raw = step_answers_map.get(str(step), {})
        if raw and not isinstance(raw, dict):
            raise SixmoFlowError(f"'step_answers.{step}' must be an object")
        if isinstance(raw, dict):
            step_answers = raw

    merged: Dict[str, Any] = {}
    merged.update(answers)
    merged.update(step_answers)
    return merged


def chrome_user_agent() -> str:
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )


def default_api_headers(user_agent: str) -> Dict[str, str]:
    return {
        "accept": "application/json, text/plain, */*",
        "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "origin": "https://sixmo.ru",
        "referer": "https://sixmo.ru/",
        "sec-ch-ua": '"Chromium";v="120", "Not-A.Brand";v="24", "Google Chrome";v="120"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": user_agent,
    }


def default_fingerprint() -> Dict[str, Any]:
    return {
        "visitorId": f"codex-{now_ms()}",
        "confidence": 0.5,
        "userAgent": chrome_user_agent(),
        "webdriver": False,
        "languages": ["ru-RU", "ru"],
        "pluginsLength": 5,
        "hardwareConcurrency": 10,
        "deviceMemory": 8,
        "screen": {"width": 1280, "height": 800, "colorDepth": 30},
        "platform": "MacIntel",
        "touchPoints": 0,
        "timezone": "Europe/Moscow",
        "hasChromeRuntime": False,
        "hasChromeObject": True,
        "vendor": "Google Inc.",
        "notificationPermission": "prompt",
        "webgl": {"vendor": "Google Inc. (Apple)", "renderer": "ANGLE (Apple, Metal)"},
        "screenConsistency": {
            "innerWidth": 1280,
            "innerHeight": 800,
            "outerWidth": 1282,
            "outerHeight": 880,
        },
        "colorDepth": 30,
        "fpComponents": [
            "fonts",
            "domBlockers",
            "fontPreferences",
            "audio",
            "screenFrame",
            "canvas",
            "osCpu",
            "languages",
            "colorDepth",
            "deviceMemory",
            "screenResolution",
            "hardwareConcurrency",
            "timezone",
            "sessionStorage",
            "localStorage",
            "indexedDB",
            "openDatabase",
            "cpuClass",
            "platform",
            "plugins",
            "touchSupport",
            "vendor",
            "vendorFlavors",
            "cookiesEnabled",
            "colorGamut",
            "invertedColors",
            "forcedColors",
            "monochrome",
            "contrast",
            "reducedMotion",
            "reducedTransparency",
            "hdr",
            "math",
            "pdfViewerEnabled",
            "architecture",
            "applePay",
            "privateClickMeasurement",
            "audioBaseLatency",
            "dateTimeLocale",
            "webGlBasics",
            "webGlExtensions",
        ],
        "hasPlaywrightBinding": False,
    }


def must_str(value: Any, error: str) -> str:
    if value is None:
        raise SixmoFlowError(error)
    text = str(value).strip()
    if not text:
        raise SixmoFlowError(error)
    return text


def as_list(value: Any, error: str) -> list[Any]:
    if not isinstance(value, list):
        raise SixmoFlowError(error)
    return value


def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as file_obj:
        data = json.load(file_obj)
    if not isinstance(data, dict):
        raise SixmoFlowError("Input JSON must be an object")
    return data


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sixmo flow automation tool (UI bootstrap + API steps).")
    parser.add_argument("--input", required=True, help="Path to input JSON payload.")
    parser.add_argument(
        "--output",
        default="",
        help="Optional path to write output JSON. If omitted, prints to stdout.",
    )
    parser.add_argument(
        "--base-url",
        default="https://sixmo.ru",
        help="Base URL for sixmo API.",
    )
    parser.add_argument(
        "--request-timeout-seconds",
        type=int,
        default=45,
        help="Timeout per HTTP request.",
    )
    parser.add_argument(
        "--poll-timeout-seconds",
        type=int,
        default=180,
        help="Max wait for a step to become ready.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Retry count for API requests.",
    )
    parser.add_argument(
        "--bootstrap-mode",
        choices=["ui", "api"],
        default="ui",
        help="How to obtain start token/cookies. 'ui' uses real browser start click (recommended).",
    )
    parser.add_argument(
        "--browser-channel",
        default="chrome",
        help="Browser channel for UI bootstrap. Use 'chrome' for local Chrome, or empty string.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run UI bootstrap in headless mode (can be less reliable for antibot checks).",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logs.")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    payload = load_json(args.input)
    if "bootstrap_mode" not in payload:
        payload["bootstrap_mode"] = args.bootstrap_mode
    if "browser_channel" not in payload:
        payload["browser_channel"] = args.browser_channel
    if args.headless:
        payload["headless"] = True

    runner = SixmoApiRunner(
        base_url=args.base_url,
        request_timeout_ms=args.request_timeout_seconds * 1000,
        poll_timeout_s=args.poll_timeout_seconds,
        max_retries=args.max_retries,
        verbose=args.verbose,
    )

    try:
        result = runner.run(payload)
    except Exception as exc:  # noqa: BLE001
        error_payload = {"ok": False, "error": str(exc)}
        text = json.dumps(error_payload, ensure_ascii=False, indent=2)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as file_obj:
                file_obj.write(text + "\n")
        else:
            print(text)
        return 1

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as file_obj:
            file_obj.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
