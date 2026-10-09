import base64
import time

import httpx

import config


def chat(messages: list[dict]) -> str:
    r = httpx.post(
        f"{config.AI_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {config.AI_API_KEY}"},
        json={
            "model": config.AI_MODEL,
            "messages": [{"role": "system", "content": config.SYSTEM_PROMPT}] + messages,
        },
        timeout=120,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def image(prompt: str) -> bytes:
    with httpx.Client(timeout=180) as c:
        r = c.post(
            f"{config.IMAGE_BASE_URL}/images/generations",
            headers={"Authorization": f"Bearer {config.IMAGE_API_KEY}"},
            json={"model": config.IMAGE_MODEL, "prompt": prompt, "n": 1},
        )
        r.raise_for_status()
        d = r.json()["data"][0]
        if d.get("b64_json"):
            return base64.b64decode(d["b64_json"])
        img = c.get(d["url"])
        img.raise_for_status()
        return img.content


def video(prompt: str) -> bytes:
    """ساخت ویدیو با Replicate؛ تا ۱۰ دقیقه منتظر می‌ماند."""
    h = {"Authorization": f"Bearer {config.REPLICATE_API_TOKEN}"}
    with httpx.Client(timeout=120, follow_redirects=True) as c:
        r = c.post(
            f"https://api.replicate.com/v1/models/{config.VIDEO_MODEL}/predictions",
            headers=h,
            json={"input": {"prompt": prompt}},
        )
        r.raise_for_status()
        pred = r.json()
        get_url = pred["urls"]["get"]
        for _ in range(120):
            if pred["status"] == "succeeded":
                break
            if pred["status"] in ("failed", "canceled"):
                raise RuntimeError(pred.get("error") or "video generation failed")
            time.sleep(5)
            pred = c.get(get_url, headers=h).json()
        else:
            raise TimeoutError("video timeout")
        out = pred["output"]
        if isinstance(out, list):
            out = out[0]
        v = c.get(out)
        v.raise_for_status()
        return v.content
