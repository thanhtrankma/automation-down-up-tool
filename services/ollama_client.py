"""Client gọi Ollama local để generate tiêu đề và mô tả."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

OLLAMA_BASE_URL = "http://localhost:11434"


def fetch_models(base_url: str = OLLAMA_BASE_URL) -> list[str]:
    req = urllib.request.Request(f"{base_url}/api/tags", method="GET")
    with urllib.request.urlopen(req, timeout=5) as response:
        data = json.loads(response.read().decode("utf-8"))
    return [m.get("name", "") for m in data.get("models", []) if m.get("name")]


def _is_broken_pipe_error(exc: BaseException) -> bool:
    if isinstance(exc, BrokenPipeError):
        return True
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 32:
        return True
    if isinstance(exc, urllib.error.URLError) and isinstance(getattr(exc, "reason", None), BrokenPipeError):
        return True
    return "broken pipe" in str(exc).lower()


def generate_text(
    model: str,
    prompt: str,
    *,
    base_url: str = OLLAMA_BASE_URL,
    temperature: float = 0.7,
    retries: int = 2,
) -> str:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            payload = json.dumps(
                {
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": temperature},
                }
            ).encode("utf-8")

            req = urllib.request.Request(
                f"{base_url}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=300) as response:
                data = json.loads(response.read().decode("utf-8"))
            return data.get("response", "").strip()
        except urllib.error.URLError as exc:
            last_error = exc
            if _is_broken_pipe_error(exc) and attempt < retries:
                continue
            raise ConnectionError(
                f"Không kết nối được Ollama tại {base_url}. Hãy mở Ollama trước rồi thử lại."
            ) from exc
        except (BrokenPipeError, OSError) as exc:
            last_error = exc
            if _is_broken_pipe_error(exc) and attempt < retries:
                continue
            raise ConnectionError(
                f"Kết nối Ollama bị ngắt (broken pipe). Hãy kiểm tra Ollama còn chạy và thử lại."
            ) from exc

    if last_error:
        raise ConnectionError("Kết nối Ollama thất bại sau nhiều lần thử.") from last_error
    return ""


def clean_ai_output(text: str) -> str:
    content = text.strip().strip('"').strip()
    if not content:
        return content

    content = re.sub(
        r"^(here is|dưới đây là|mô tả gợi ý|gợi ý mô tả|đây là|tiêu đề gợi ý)\b.*?:\s*",
        "",
        content,
        flags=re.IGNORECASE | re.DOTALL,
    ).strip()

    content = re.sub(
        r"\n*(let me know if you need.*|nếu bạn cần.*|bạn muốn tôi.*)$",
        "",
        content,
        flags=re.IGNORECASE | re.DOTALL,
    ).strip()

    if (content.startswith('"') and content.endswith('"')) or (
        content.startswith("'") and content.endswith("'")
    ):
        content = content[1:-1].strip()

    return content.strip()


def sanitize_youtube_title(text: str, fallback: str = "Video") -> str:
    """Chuẩn hoá tiêu đề hợp lệ cho YouTube (không rỗng, tối đa 100 ký tự)."""
    for candidate in (text, fallback, "Video"):
        # Tiêu đề chỉ lấy dòng đầu, gom khoảng trắng
        title = re.sub(r"\s+", " ", (candidate or "").split("\n")[0].strip())
        if title:
            return title[:100].rstrip()
    return "Video"


def extract_keywords(text: str) -> str:
    cleaned = text.replace("⧸", " ").replace("｜", " ").replace("|", " ")
    cleaned = re.sub(r"[_/\-]+", " ", cleaned)
    cleaned = re.sub(r"[^\w\sÀ-ỹ]", " ", cleaned, flags=re.UNICODE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return ""
    words = [w for w in cleaned.split(" ") if len(w) > 1]
    return ", ".join(words[:12])


def generate_title(model: str, original_title: str, title_instruction: str = "") -> str:
    fallback = sanitize_youtube_title(original_title, "Video")
    extra = f"\nHướng dẫn thêm: {title_instruction}" if title_instruction.strip() else ""
    prompt = (
        "Bạn là chuyên gia đặt tiêu đề YouTube bằng tiếng Việt. "
        "YÊU CẦU BẮT BUỘC: Chỉ trả về đúng 1 tiêu đề cuối cùng trên MỘT DÒNG, không lời dẫn, không ghi chú, không hỏi thêm. "
        "Viết lại tiêu đề gốc theo cách khác nhưng vẫn giữ đúng ý nghĩa, hấp dẫn và tự nhiên. "
        "Không copy y nguyên tiêu đề gốc. Độ dài tối đa 100 ký tự. "
        f"Tiêu đề gốc: {fallback}{extra}\n"
        "Đầu ra chỉ là tiêu đề hoàn chỉnh."
    )
    try:
        result = clean_ai_output(generate_text(model, prompt, temperature=0.8))
    except Exception:
        result = ""
    return sanitize_youtube_title(result, fallback)


def generate_description(model: str, title: str, keywords: str) -> str:
    prompt = (
        "Bạn là chuyên gia viết mô tả YouTube bằng tiếng Việt. "
        "YÊU CẦU BẮT BUỘC: Chỉ trả về đúng phần mô tả cuối cùng bằng tiếng Việt, không lời dẫn, "
        "không mở đầu kiểu 'Here is...', không ghi chú, không hỏi thêm, không đặt trong dấu ngoặc kép. "
        "Viết 4-6 câu, giọng tự nhiên, giàu hình ảnh, có 1 câu kêu gọi hành động cuối đoạn. "
        "Không bịa thông tin không có trong tiêu đề/từ khoá. "
        f"Tiêu đề video: {title}\n"
        f"Từ khoá: {keywords}\n"
        "Đầu ra chỉ là đoạn mô tả hoàn chỉnh."
    )
    try:
        return clean_ai_output(generate_text(model, prompt))
    except Exception:
        fallback = title
        if keywords:
            fallback += f"\n\n{keywords}"
        return fallback
