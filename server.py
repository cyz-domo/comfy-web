"""comfy-web: 自然语言 -> ComfyUI 文生图 / 文生视频 的本地后端。

只依赖 Python 标准库。前端页面在 index.html，产物落在 ./outputs。
配置优先级：环境变量 > config.json > 内置默认值。
"""

import json
import mimetypes
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import psutil
except ImportError:
    psutil = None

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, "outputs")
CONFIG_PATH = os.path.join(HERE, "config.json")
os.makedirs(OUTPUT_DIR, exist_ok=True)


def load_config():
    cfg = {
        "comfy_url": "http://127.0.0.1:8188",
        "comfy_dir": "E:/ComfyUI",
        "image_checkpoint": "sd_xl_base_1.0.safetensors",
        "video_checkpoint": "ltx-video-2b-v0.9.5.safetensors",
        "video_text_encoder": "t5xxl_fp8_e4m3fn_scaled.safetensors",
        "llm": {"enabled": True, "base_url": "", "api_key": "", "model": ""},
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                user = json.load(f)
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception as e:
            print(f"[WARN] 读取 config.json 失败: {e}")
    cfg["comfy_url"] = os.environ.get("COMFY_URL", cfg["comfy_url"]).rstrip("/")
    cfg["comfy_dir"] = os.environ.get("COMFY_DIR", cfg["comfy_dir"])
    llm = cfg["llm"]
    llm["base_url"] = os.environ.get("LLM_BASE_URL", llm.get("base_url", "")).rstrip("/")
    llm["api_key"] = os.environ.get("LLM_API_KEY", llm.get("api_key", ""))
    llm["model"] = os.environ.get("LLM_MODEL", llm.get("model", ""))
    return cfg


def save_config():
    """把当前配置写回 config.json（含 api_key）。该文件已在 .gitignore 中。"""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(CONFIG, f, ensure_ascii=False, indent=2)


def llm_ready():
    llm = CONFIG["llm"]
    return bool(llm.get("enabled") and llm.get("api_key") and llm.get("base_url") and llm.get("model"))


def mask_key(key):
    if not key:
        return ""
    return "****" + key[-4:] if len(key) > 8 else "****"


def llm_view():
    llm = CONFIG["llm"]
    return {
        "enabled": bool(llm.get("enabled")),
        "base_url": llm.get("base_url", ""),
        "model": llm.get("model", ""),
        "api_key_masked": mask_key(llm.get("api_key", "")),
        "has_key": bool(llm.get("api_key")),
        "ready": llm_ready(),
    }


CONFIG = load_config()
INPUT_DIR = os.path.join(CONFIG["comfy_dir"].replace("\\", "/"), "input")
JOBS = {}
LOCK = threading.Lock()

SYSTEM_METRICS = {
    "cpu": {"percent": 0.0, "count": os.cpu_count() or 1},
    "ram": {"used_gb": 0.0, "total_gb": 0.0, "percent": 0.0},
    "gpu": {
        "available": False,
        "name": "NVIDIA GPU",
        "full_name": "NVIDIA GPU",
        "util": 0.0,
        "vram_used_mb": 0,
        "vram_total_mb": 0,
        "vram_used_gb": 0.0,
        "vram_total_gb": 0.0,
        "vram_percent": 0.0,
        "power_watts": 0.0,
        "temp": 0.0,
    },
    "comfy": False,
    "timestamp": 0,
}


def monitor_loop():
    """后台采集 CPU、内存、GPU 显存/功耗/负载/温度，每 1.5 秒更新一次缓存。"""
    if psutil:
        try:
            psutil.cpu_percent(interval=None)
        except Exception:
            pass
    while True:
        try:
            time.sleep(1.5)
            cpu_p = psutil.cpu_percent(interval=None) if psutil else 0.0
            mem = psutil.virtual_memory() if psutil else None

            gpu = {"available": False}
            try:
                res = subprocess.check_output(
                    [
                        "nvidia-smi",
                        "--query-gpu=name,utilization.gpu,memory.used,memory.total,power.draw,temperature.gpu",
                        "--format=csv,noheader,nounits",
                    ],
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    timeout=2,
                ).decode("utf-8", "ignore").strip()
                if res:
                    parts = [p.strip() for p in res.split(",")]
                    raw_name = parts[0]
                    short_name = raw_name.replace("NVIDIA GeForce ", "").replace("Laptop GPU", "").strip()
                    util = float(parts[1]) if parts[1] != "[N/A]" else 0.0
                    used = float(parts[2]) if parts[2] != "[N/A]" else 0.0
                    total = float(parts[3]) if parts[3] != "[N/A]" else 4096.0
                    power = float(parts[4]) if parts[4] != "[N/A]" else 0.0
                    temp = float(parts[5]) if parts[5] != "[N/A]" else 0.0
                    gpu = {
                        "available": True,
                        "name": short_name,
                        "full_name": raw_name,
                        "util": round(util, 1),
                        "vram_used_mb": round(used),
                        "vram_total_mb": round(total),
                        "vram_used_gb": round(used / 1024, 1),
                        "vram_total_gb": round(total / 1024, 1),
                        "vram_percent": round(used / max(1.0, total) * 100, 1),
                        "power_watts": round(power, 1),
                        "temp": round(temp, 1),
                    }
            except Exception:
                pass

            comfy_ok = False
            try:
                st = comfy_get("/system_stats", timeout=1.5)
                comfy_ok = True
                if not gpu.get("available") and st.get("devices"):
                    dev = st["devices"][0]
                    v_tot = dev.get("vram_total", 0) / (1024**2)
                    v_free = dev.get("vram_free", 0) / (1024**2)
                    v_used = max(0, v_tot - v_free)
                    gpu = {
                        "available": True,
                        "name": dev.get("name", "GPU").split(":")[-1].strip(),
                        "full_name": dev.get("name", "GPU"),
                        "util": 0.0,
                        "vram_used_mb": round(v_used),
                        "vram_total_mb": round(v_tot),
                        "vram_used_gb": round(v_used / 1024, 1),
                        "vram_total_gb": round(v_tot / 1024, 1),
                        "vram_percent": round(v_used / max(1.0, v_tot) * 100, 1),
                        "power_watts": 0.0,
                        "temp": 0.0,
                    }
            except Exception:
                comfy_ok = False

            global SYSTEM_METRICS
            SYSTEM_METRICS = {
                "cpu": {
                    "percent": round(cpu_p, 1),
                    "count": os.cpu_count() or 1,
                },
                "ram": {
                    "used_gb": round(mem.used / 1024**3, 1) if mem else 0.0,
                    "total_gb": round(mem.total / 1024**3, 1) if mem else 0.0,
                    "percent": round(mem.percent, 1) if mem else 0.0,
                },
                "gpu": gpu,
                "comfy": comfy_ok,
                "timestamp": time.time(),
            }
        except Exception:
            pass


CJK = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")

DEFAULT_NEG_IMAGE = (
    "watermark, text, blurry, low quality, jpeg artifacts, deformed, extra limbs, "
    "bad anatomy, bad hands, missing fingers, poorly drawn face, disfigured, mutated"
)
DEFAULT_NEG_VIDEO = (
    "low quality, worst quality, deformed, distorted, disfigured, motion smear, "
    "motion artifacts, fused fingers, bad anatomy, weird hand, ugly, jerky motion, flickering"
)

# 多风格 AI 提示词工程模板（针对 SDXL & LTX-Video 专业调优）
PROMPT_STYLES = {
    "photorealistic": {
        "name": "写实摄影 (Photorealistic)",
        "desc": "强化单反镜头感、真实皮肤/材质细节与自然光影",
        "system": (
            "You are an expert AI prompt engineer specializing in SDXL photorealistic photography prompts. "
            "Convert the user's Chinese concept into a high-detail English image prompt. "
            "Include: subject description, camera lens (e.g. 35mm/85mm, f/1.4), lighting (e.g. soft volumetric lighting, golden hour, rim light), "
            "composition, rich texture details, 8k resolution, photorealistic masterpiece. "
            "Output ONLY the English prompt as comma-separated descriptive tags. Do NOT explain. No quotes, no markdown."
        ),
    },
    "cinematic": {
        "name": "电影质感 (Cinematic)",
        "desc": "强化电影画面构图、胶片质感、氛围光与戏剧感",
        "system": (
            "You are an expert AI prompt engineer for cinematic imagery. "
            "Convert the user's Chinese concept into an evocative English prompt with movie still aesthetics. "
            "Include: dramatic atmospheric lighting, anamorphic lens, depth of field, color grading, cinematic shadows, masterpiece, 8k. "
            "Output ONLY the English prompt as comma-separated descriptive tags. Do NOT explain. No quotes, no markdown."
        ),
    },
    "anime": {
        "name": "二次元动漫 (Anime)",
        "desc": "强化日系动漫笔触、清亮色彩与唯美光效",
        "system": (
            "You are an expert AI prompt engineer for anime and manga art styles. "
            "Convert the user's Chinese concept into an expressive English prompt for anime models. "
            "Include: anime aesthetic, vibrant colors, expressive character traits, clean linework, Makoto Shinkai lighting, highly detailed background, masterpiece. "
            "Output ONLY the English prompt as comma-separated descriptive tags. Do NOT explain. No quotes, no markdown."
        ),
    },
    "fantasy": {
        "name": "奇幻概念艺术 (Concept Art)",
        "desc": "强化幻想艺术、宏大场景、魔法微粒与画师质感",
        "system": (
            "You are an expert AI prompt engineer for fantasy and digital concept art. "
            "Convert the user's Chinese concept into an imaginative English prompt. "
            "Include: epic fantasy composition, digital painting, glowing magical elements, intricate textures, trending on ArtStation, volumetric light, highly detailed. "
            "Output ONLY the English prompt as comma-separated descriptive tags. Do NOT explain. No quotes, no markdown."
        ),
    },
    "video_motion": {
        "name": "视频动态运镜 (LTX-Video 专向)",
        "desc": "专为视频模型定制，强化镜头运镜轨迹、物理动态与时间连续性",
        "system": (
            "You are an expert AI prompt engineer specifically for LTX-Video and text-to-video diffusion models. "
            "Convert the user's Chinese concept into a vivid English prompt emphasizing motion and camera movement. "
            "Describe the subject's subtle action, natural environmental movement (e.g., wind blowing, water flowing), "
            "and camera work (e.g., slow cinematic pan, subtle dolly zoom, steady tracking shot), smooth motion, 24fps film look. "
            "Output ONLY the English prompt as comma-separated descriptive tags. Do NOT explain. No quotes, no markdown."
        ),
    },
    "direct": {
        "name": "精准直译 (Direct Tagging)",
        "desc": "忠实直译，不额外添加过度发散的描述",
        "system": (
            "You are an accurate translator and tagger for AI image generation. "
            "Translate the user's Chinese description faithfully into concise English comma-separated tags and phrases. "
            "Preserve original subject, actions, lighting, and style without inventing unrelated elements. "
            "Output ONLY the English tags. Do NOT explain. No quotes, no markdown."
        ),
    },
}


def clean_llm_output(text):
    """清洗 LLM 输出，剔除思考过程、Markdown 标记、多余前缀与空白。"""
    if not text:
        return ""
    # 过滤 reasoning 模型的 <think>...</think>
    text = re.sub(r"<think>[\s\S]*?</think>", "", text)
    # 过滤 Markdown 代码块标记 ```markdown ... ```
    text = re.sub(r"```(?:markdown|prompt|text)?([\s\S]*?)```", r"\1", text)
    # 过滤常见前缀
    text = re.sub(r"^(?:Prompt|English Prompt|Translation|Output|Here is[^:]*|改写结果|优化提示词|提示词)[:：]\s*", "", text, flags=re.IGNORECASE)
    # 剔除首尾双引号或单引号
    text = text.strip().strip('"`')
    # 拆分行，以逗号连接
    lines = [line.strip().strip(",") for line in text.split("\n") if line.strip()]
    cleaned = ", ".join(lines)
    # 规范化多重逗号与空格
    cleaned = re.sub(r",\s*,+", ", ", cleaned)
    return cleaned.strip(" ,")


# ---------- ComfyUI 工作流 ----------

def wf_image(prompt, width, height, steps, seed, init=None, denoise=1.0, neg=None, cfg=6.0):
    """init=None 走纯文生图；init 给了 ComfyUI input 目录里的文件名则走以图生图。"""
    negative = neg.strip() if neg and neg.strip() else DEFAULT_NEG_IMAGE
    if init:
        latent = {
            "4": {"class_type": "LoadImage", "inputs": {"image": init}},
            "8": {
                "class_type": "ImageScale",
                "inputs": {
                    "image": ["4", 0],
                    "upscale_method": "bicubic",
                    "width": width,
                    "height": height,
                    "crop": "disabled",
                },
            },
            "9": {"class_type": "VAEEncode", "inputs": {"pixels": ["8", 0], "vae": ["1", 2]}},
        }
        latent_src = ["9", 0]
    else:
        latent = {
            "4": {
                "class_type": "EmptyLatentImage",
                "inputs": {"width": width, "height": height, "batch_size": 1, "seed": seed},
            }
        }
        latent_src = ["4", 0]
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": CONFIG["image_checkpoint"]}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["1", 1]}},
        **latent,
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": latent_src,
                "seed": seed,
                "steps": steps,
                "cfg": float(cfg),
                "sampler_name": "dpmpp_2m_sde",
                "scheduler": "karras",
                "denoise": denoise,
            },
        },
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": "comfyweb/img"}},
    }


def wf_video(prompt, image_name, width, height, length, steps, seed, strength=1.0, neg=None, cfg=3.0):
    negative = neg.strip() if neg and neg.strip() else DEFAULT_NEG_VIDEO
    return {
        "10": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": CONFIG["video_checkpoint"]}},
        "11": {"class_type": "CLIPLoader", "inputs": {"clip_name": CONFIG["video_text_encoder"], "type": "ltxv"}},
        "12": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["11", 0]}},
        "13": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["11", 0]}},
        "14": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "15": {
            "class_type": "LTXVImgToVideo",
            "inputs": {
                "positive": ["12", 0],
                "negative": ["13", 0],
                "vae": ["10", 2],
                "image": ["14", 0],
                "width": width,
                "height": height,
                "length": length,
                "batch_size": 1,
                "strength": strength,
            },
        },
        "16": {"class_type": "LTXVConditioning", "inputs": {"positive": ["15", 0], "negative": ["15", 1], "frame_rate": 25}},
        "17": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "18": {
            "class_type": "LTXVScheduler",
            "inputs": {
                "steps": steps,
                "max_shift": 2.05,
                "base_shift": 0.95,
                "stretch": True,
                "terminal": 0.1,
                "latent": ["15", 2],
            },
        },
        "19": {
            "class_type": "SamplerCustom",
            "inputs": {
                "model": ["10", 0],
                "add_noise": True,
                "noise_seed": seed,
                "cfg": float(cfg),
                "positive": ["16", 0],
                "negative": ["16", 1],
                "sampler": ["17", 0],
                "sigmas": ["18", 0],
                "latent_image": ["15", 2],
            },
        },
        "20": {"class_type": "VAEDecode", "inputs": {"samples": ["19", 0], "vae": ["10", 2]}},
        "21": {"class_type": "CreateVideo", "inputs": {"images": ["20", 0], "fps": 24}},
        "22": {
            "class_type": "SaveVideo",
            "inputs": {"video": ["21", 0], "filename_prefix": "comfyweb/vid", "format": "auto", "codec": "auto"},
        },
    }


# ---------- HTTP 基础 ----------

def http_json(url, payload=None, headers=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    hdrs = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ComfyWeb/1.0",
    }
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        err_msg = ""
        try:
            err_body = e.read().decode()
            err_json = json.loads(err_body)
            if "error" in err_json:
                if isinstance(err_json["error"], dict):
                    err_msg = err_json["error"].get("message") or str(err_json["error"])
                else:
                    err_msg = str(err_json["error"])
            else:
                err_msg = err_body[:200]
        except Exception:
            err_msg = f"HTTP {e.code}: {e.reason}"
        raise RuntimeError(f"API请求失败 ({e.code}): {err_msg}")


def comfy_get(path, timeout=30):
    return http_json(CONFIG["comfy_url"] + path, timeout=timeout)


def comfy_post(path, payload, timeout=60):
    return http_json(CONFIG["comfy_url"] + path, payload, timeout=timeout)


IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")


def walk_images(root, limit=60):
    """本地目录里的图片，按修改时间从新到旧；subfolder 用相对路径表示。"""
    found = []
    if not os.path.exists(root):
        return []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".cache", "__pycache__")]
        for fn in filenames:
            if not fn.lower().endswith(IMG_EXT):
                continue
            full = os.path.join(dirpath, fn)
            try:
                st = os.stat(full)
            except OSError:
                continue
            rel = os.path.relpath(dirpath, root).replace("\\", "/")
            found.append((st.st_mtime, full, fn, "" if rel == "." else rel))
    found.sort(reverse=True)
    return [{"abs": f, "name": n, "subfolder": s} for _, f, n, s in found[:limit]]


def list_presets(source):
    """列出能直接选来当参考图/首帧的现成图片。"""
    root_map = {
        "ours": OUTPUT_DIR,
        "input": INPUT_DIR,
        "output": os.path.join(CONFIG["comfy_dir"].replace("\\", "/"), "output"),
    }
    root = root_map.get(source, OUTPUT_DIR)
    return [{"source": source, "name": it["name"], "subfolder": it["subfolder"]} for it in walk_images(root)]


def list_gallery(limit=100):
    """列出 outputs/ 目录下的所有生成产物，包含元信息，按修改时间倒序。"""
    found = []
    if not os.path.exists(OUTPUT_DIR):
        return []
    for fn in os.listdir(OUTPUT_DIR):
        ext = os.path.splitext(fn)[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp", ".mp4"):
            continue
        full = os.path.join(OUTPUT_DIR, fn)
        try:
            st = os.stat(full)
        except OSError:
            continue
        is_video = ext == ".mp4"
        found.append({
            "file": fn,
            "type": "video" if is_video else "image",
            "size": st.st_size,
            "mtime": st.st_mtime,
            "url": f"/outputs/{fn}",
        })
    found.sort(key=lambda x: x["mtime"], reverse=True)
    return found[:limit]


def get_styles():
    return [{"id": k, "name": v["name"], "desc": v["desc"]} for k, v in PROMPT_STYLES.items()]


def fetch_llm_models(base_url, api_key):
    """从 OpenAI 兼容接口获取模型列表"""
    if not base_url or not api_key:
        return []
    req = urllib.request.Request(
        base_url.rstrip("/") + "/models",
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ComfyWeb/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read().decode())
        models = [m.get("id") for m in data.get("data", []) if m.get("id")]
        return models


def ref_blob(ref):
    """按 {source,name,subfolder} 把图片字节取回来。"""
    if ref["source"] == "ours":
        full = os.path.join(OUTPUT_DIR, os.path.basename(ref["name"]))
        if not os.path.isfile(full):
            raise FileNotFoundError(ref["name"])
        with open(full, "rb") as f:
            return f.read()
    q = urllib.parse.urlencode({"filename": ref["name"], "subfolder": ref.get("subfolder", ""), "type": ref["source"]})
    return fetch_binary(CONFIG["comfy_url"] + "/view?" + q, timeout=30)


def stage_ref(ref, job):
    """把选中的参考图落到 ComfyUI 的 input 目录。"""
    if ref["source"] == "input" and not ref.get("subfolder"):
        return ref["name"], False
    name = f"{job['id']}_ref{os.path.splitext(ref['name'])[1].lower() or '.png'}"
    with open(os.path.join(INPUT_DIR, name), "wb") as f:
        f.write(ref_blob(ref))
    return name, True


def unstage(name, temp):
    if temp:
        try:
            os.remove(os.path.join(INPUT_DIR, name))
        except OSError:
            pass


def fetch_binary(url, timeout=120):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def parse_multipart(blob, boundary):
    """multipart/form-data 解析，返回 [(字段名, (文件名, 字节)), ...]。"""
    out = []
    for part in blob.split(b"--" + boundary):
        part = part.lstrip(b"\r\n")
        if not part or part.startswith(b"--"):
            continue
        head, sep, body = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        if body.endswith(b"\r\n"):
            body = body[:-2]
        name = filename = ""
        for line in head.decode("utf-8", "replace").split("\r\n"):
            hit = re.search(r'name="([^"]*)"', line)
            if hit:
                name = hit.group(1)
            hit = re.search(r'filename="([^"]*)"', line)
            if hit:
                filename = os.path.basename(hit.group(1).replace("\\", "/"))
        if name:
            out.append((name, (filename, body)))
    return out


# ---------- 中文 -> 英文提示词工程 ----------

def translate(text, style="photorealistic", mode="image", job=None):
    """返回 (英文提示词, 是否经过 LLM 改写, 错误信息)。不可用时原样透传。"""
    if not llm_ready():
        return text, False, ""
    
    has_cjk = bool(CJK.search(text))
    # 纯英文且选用 direct 模式时直接透传，其余模式仍可通过 LLM 进行风格扩写
    if not has_cjk and style == "direct":
        return text, False, ""

    # 若是视频模式且未显式指定风格，默认采用视频运镜
    if mode == "video" and style == "photorealistic":
        style = "video_motion"

    llm = CONFIG["llm"]
    style_info = PROMPT_STYLES.get(style, PROMPT_STYLES["photorealistic"])
    system_prompt = style_info["system"]

    body = {
        "model": llm["model"],
        "temperature": 0.6 if style != "direct" else 0.2,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"User Concept:\n{text}"},
        ],
    }
    hdrs = {
        "Authorization": f"Bearer {llm['api_key']}",
    }
    try:
        data = http_json(llm["base_url"] + "/chat/completions", body, headers=hdrs, timeout=60)
        out = data["choices"][0]["message"]["content"].strip()
        cleaned = clean_llm_output(out)
        if not cleaned:
            return text, False, "LLM 返回空内容"
        return cleaned, True, ""
    except Exception as e:
        err = f"{e}"
        if job is not None:
            job["logs"].append(f"LLM 改写失败，按原文继续：{err}")
        return text, False, err


# ---------- 任务执行 ----------

def wait_for_output(prompt_id, node_id, timeout=1800):
    t0 = time.time()
    while time.time() - t0 < timeout:
        hist = comfy_get(f"/history/{prompt_id}", timeout=30)
        entry = hist.get(prompt_id)
        if entry:
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                msgs = [m for m in status.get("messages", []) if m[0] == "execution_error"]
                raise RuntimeError(f"ComfyUI 执行出错: {json.dumps(msgs, ensure_ascii=False)[:800]}")
            out = entry.get("outputs", {}).get(node_id)
            if out:
                item = (out.get("images") or out.get("videos") or [None])[0]
                if item:
                    return item, time.time() - t0
        time.sleep(2)
    raise TimeoutError("等待 ComfyUI 结果超时")


def pull(item, prefix):
    """把 ComfyUI 产物拉到本地 outputs 目录，返回 (文件名, 元信息)。"""
    q = urllib.parse.urlencode({"filename": item["filename"], "subfolder": item.get("subfolder", ""), "type": item.get("type", "output")})
    blob = fetch_binary(f"{CONFIG['comfy_url']}/view?{q}")
    ext = os.path.splitext(item["filename"])[1] or ".png"
    name = f"{prefix}{ext}"
    with open(os.path.join(OUTPUT_DIR, name), "wb") as f:
        f.write(blob)
    meta = item.get("metadata") or {}
    return name, {
        "bytes": len(blob),
        "width": meta.get("width"),
        "height": meta.get("height"),
        "duration": meta.get("duration"),
        "fps": meta.get("fps"),
        "frame_count": meta.get("frame_count"),
    }


def snap8(v):
    """VAEEncode 只吃 8 的倍数，按浏览器报回来的原始尺寸对齐。"""
    return max(64, int(v) // 8 * 8)


def stage_image(job, prompt, params, seed, ref=None):
    """ref=None 是纯文生图；ref 给定则拿那张图做以图生图。"""
    if ref:
        w = snap8(params["ref_width"] or params["width"])
        h = snap8(params["ref_height"] or params["height"])
        init, temp = stage_ref(ref, job)
    else:
        w, h, init, temp = params["width"], params["height"], None, False
    try:
        wf = wf_image(
            prompt,
            w,
            h,
            params["steps"],
            seed,
            init=init,
            denoise=params["denoise"] if init else 1.0,
            neg=params.get("negative_prompt"),
            cfg=params.get("cfg", 6.0),
        )
        pid = comfy_post("/prompt", {"prompt": wf, "client_id": job["id"]})["prompt_id"]
        job["logs"].append(f"{'以图生图' if init else '文生图'}已提交 {w}x{h} prompt_id={pid}")
        item, secs = wait_for_output(pid, "7")
        name, meta = pull(item, f"{job['id']}_img")
        job["image"] = {"file": name, "seconds": round(secs, 1), **meta}
        job["logs"].append(f"生图完成 {round(secs, 1)}s -> {name}")
        return name
    finally:
        if ref:
            unstage(init, temp)


def stage_video(job, prompt, params, seed, ref):
    name, temp = stage_ref(ref, job)
    try:
        wf = wf_video(
            prompt,
            name,
            params["v_width"],
            params["v_height"],
            params["length"],
            params["v_steps"],
            seed,
            params["strength"],
            neg=params.get("negative_prompt"),
            cfg=params.get("v_cfg", 3.0),
        )
        pid = comfy_post("/prompt", {"prompt": wf, "client_id": job["id"]})["prompt_id"]
        job["logs"].append(f"图生视频已提交 prompt_id={pid}")
        item, secs = wait_for_output(pid, "22")
        fname, meta = pull(item, f"{job['id']}_vid")
        job["video"] = {"file": fname, "seconds": round(secs, 1), **meta}
        job["logs"].append(f"图生视频完成 {round(secs, 1)}s -> {fname}")
    finally:
        unstage(name, temp)


def run_job(job):
    params, ref = job["params"], job["ref"]
    try:
        prompt_en = (job.get("prompt_en") or "").strip()
        used_llm = False
        llm_err = ""

        # 如果前端没有显式提供已确认的 prompt_en，则触发自动改写
        if not prompt_en:
            style = job.get("style", "photorealistic")
            job["logs"].append(f"正在进行提示词优化与翻译 (风格: {style})...")
            prompt_en, used_llm, llm_err = translate(job["prompt_raw"], style=style, mode=job["mode"], job=job)
            if used_llm:
                job["logs"].append(f"LLM 优化提示词完成 -> {prompt_en[:120]}...")
        else:
            used_llm = True
            job["logs"].append(f"使用已确认的英文提示词 -> {prompt_en[:120]}...")

        job["llm_error"] = llm_err
        job["prompt_en"] = prompt_en
        job["llm_used"] = used_llm
        seed = params.pop("seed") or int(time.time()) % (2**31)
        job["seed"] = seed

        if job["mode"] == "video" and ref is None:
            job["logs"].append("文生视频 = 两步串联：SDXL 先出图，LTX-Video 再让它动起来")
            img = stage_image(job, prompt_en, params, seed)
            ref = {"source": "ours", "name": img, "subfolder": ""}
        else:
            img = stage_image(job, prompt_en, params, seed, ref)

        if job["mode"] == "video":
            stage_video(job, prompt_en, params, seed, ref)

        job["state"] = "done"
    except Exception as e:
        job["state"] = "error"
        job["error"] = f"{type(e).__name__}: {e}"
        job["logs"].append(f"任务出错: {job['error']}")
    finally:
        job["finished_at"] = time.time()
        with LOCK:
            JOBS[job["id"]]["logs"] = list(job["logs"])


def new_job(mode, prompt, params, ref, prompt_en="", style="photorealistic"):
    job = {
        "id": uuid.uuid4().hex[:12],
        "mode": mode,
        "prompt_raw": prompt,
        "prompt_en": prompt_en,
        "style": style,
        "ref": ref,
        "params": params,
        "state": "running",
        "logs": [],
        "image": None,
        "video": None,
        "started_at": time.time(),
    }
    with LOCK:
        JOBS[job["id"]] = job
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return job["id"]


# ---------- 路由 ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, code, obj):
        blob = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def send_blob(self, code, blob, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                self.send_blob(200, f.read(), "text/html; charset=utf-8")
        elif path.startswith("/outputs/"):
            name = os.path.basename(path)
            full = os.path.join(OUTPUT_DIR, name)
            if not os.path.isfile(full):
                self.send_json(404, {"error": "not found"})
                return
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            with open(full, "rb") as f:
                self.send_blob(200, f.read(), ctype)
        elif path == "/api/status":
            try:
                st = comfy_get("/system_stats", timeout=5)
                dev = (st.get("devices") or [{}])[0]
                self.send_json(
                    200,
                    {
                        "comfy": True,
                        "gpu": dev.get("name"),
                        "vram_gb": round((dev.get("vram_total") or 0) / 1024**3, 1),
                        "llm": llm_view(),
                        "models": {
                            "image": CONFIG["image_checkpoint"],
                            "video": CONFIG["video_checkpoint"],
                        },
                    },
                )
            except Exception as e:
                self.send_json(200, {"comfy": False, "error": str(e), "llm": llm_view()})
        elif path == "/api/settings":
            self.send_json(200, llm_view())
        elif path == "/api/presets":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            source = (q.get("source") or ["ours"])[0]
            if source not in ("ours", "input", "output"):
                self.send_json(400, {"error": "source 只能是 ours / input / output"})
                return
            self.send_json(200, list_presets(source))
        elif path == "/api/gallery":
            self.send_json(200, list_gallery())
        elif path == "/api/monitor":
            self.send_json(200, SYSTEM_METRICS)
        elif path == "/api/styles":
            self.send_json(200, get_styles())
        elif path == "/api/preview":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            ref = {
                "source": (q.get("source") or [""])[0],
                "name": (q.get("name") or [""])[0],
                "subfolder": (q.get("subfolder") or [""])[0],
            }
            try:
                blob = ref_blob(ref)
            except Exception as e:
                self.send_json(404, {"error": f"{type(e).__name__}: {e}"})
                return
            ctype = mimetypes.guess_type(ref["name"])[0] or "image/png"
            self.send_blob(200, blob, ctype)
        elif path.startswith("/api/jobs/"):
            jid = path.rsplit("/", 1)[-1]
            with LOCK:
                job = JOBS.get(jid)
                snapshot = dict(job) if job else None
            if not snapshot:
                self.send_json(404, {"error": "no such job"})
                return
            snapshot.pop("params", None)
            self.send_json(200, snapshot)
        elif path == "/api/jobs":
            with LOCK:
                items = [dict(v) for v in JOBS.values()]
            for it in items:
                it.pop("params", None)
            items.sort(key=lambda x: x["started_at"], reverse=True)
            self.send_json(200, items[:50])
        else:
            self.send_json(404, {"error": "not found"})

    def read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode() or "{}")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/upload":
            self.handle_upload()
            return
        try:
            body = self.read_body()
        except Exception as e:
            self.send_json(400, {"error": f"请求体不是合法 JSON：{type(e).__name__}: {e}"})
            return

        if path == "/api/jobs":
            self.handle_job(body)
        elif path == "/api/prompt/optimize":
            self.handle_prompt_optimize(body)
        elif path == "/api/settings":
            self.handle_settings(body)
        elif path == "/api/settings/test":
            self.handle_settings_test(body)
        elif path == "/api/settings/models":
            self.handle_settings_models(body)
        else:
            self.send_json(404, {"error": "not found"})

    def handle_upload(self):
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            self.send_json(400, {"error": "需要 multipart/form-data"})
            return
        boundary = ""
        for piece in ctype.split(";"):
            piece = piece.strip()
            if piece.startswith("boundary="):
                boundary = piece.split("=", 1)[1].strip('"')
        if not boundary:
            self.send_json(400, {"error": "缺少 multipart boundary"})
            return
        n = int(self.headers.get("Content-Length") or 0)
        if n > 30 * 1024 * 1024:
            self.send_json(400, {"error": "图片太大（上限 30 MB）"})
            return
        blob = self.rfile.read(n)
        fields = parse_multipart(blob, boundary.encode())
        upload = next((v for k, v in fields if k == "file" and v[1]), None)
        if not upload:
            self.send_json(400, {"error": "没收到文件字段 file"})
            return
        filename, content = upload
        ext = os.path.splitext(filename)[1].lower()
        if ext not in IMG_EXT:
            self.send_json(400, {"error": f"只支持 {'/'.join(IMG_EXT)}，收到 {ext or '无后缀'}"})
            return
        name = f"{uuid.uuid4().hex[:8]}_up{ext}"
        with open(os.path.join(OUTPUT_DIR, name), "wb") as f:
            f.write(content)
        self.send_json(200, {"source": "ours", "name": name, "subfolder": ""})

    def handle_prompt_optimize(self, body):
        prompt = (body.get("prompt") or "").strip()
        style = (body.get("style") or "photorealistic").strip()
        mode = (body.get("mode") or "image").strip()
        if not prompt:
            self.send_json(400, {"error": "提示词不能为空"})
            return
        if not llm_ready():
            self.send_json(200, {
                "ok": False,
                "error": "LLM 未就绪：请先在设置中填写并保存 Base URL、Model 与 API Key",
                "prompt_raw": prompt,
                "prompt_en": prompt,
                "style": style,
                "used": False,
            })
            return
        out, used, err = translate(prompt, style=style, mode=mode)
        self.send_json(200, {
            "ok": not bool(err),
            "prompt_raw": prompt,
            "prompt_en": out,
            "style": style,
            "used": used,
            "error": err,
        })

    def handle_settings_models(self, body):
        base_url = (body.get("base_url") or CONFIG["llm"].get("base_url") or "").strip().rstrip("/")
        api_key = (body.get("api_key") or CONFIG["llm"].get("api_key") or "").strip()
        if api_key.startswith("****"):
            api_key = CONFIG["llm"].get("api_key", "")
        if not base_url or not api_key:
            self.send_json(200, {"ok": False, "error": "需要填写 Base URL 与 API Key", "models": []})
            return
        try:
            models = fetch_llm_models(base_url, api_key)
            self.send_json(200, {"ok": True, "models": models})
        except Exception as e:
            self.send_json(200, {"ok": False, "error": str(e), "models": []})

    def handle_job(self, body):
        try:
            mode = body.get("mode", "image")
            prompt = (body.get("prompt") or "").strip()
            prompt_en = (body.get("prompt_en") or "").strip()
            style = (body.get("style") or "photorealistic").strip()
            negative_prompt = (body.get("negative_prompt") or "").strip()

            if not prompt and not prompt_en:
                raise ValueError("提示词不能为空")
            if mode not in ("image", "video"):
                raise ValueError("mode 只能是 image 或 video")

            params = {
                "width": int(body.get("width", 1024)),
                "height": int(body.get("height", 1024)),
                "steps": int(body.get("steps", 25)),
                "seed": int(body.get("seed", 0)),
                "cfg": float(body.get("cfg", 6.0)),
                "v_width": int(body.get("v_width", 768)),
                "v_height": int(body.get("v_height", 512)),
                "length": int(body.get("length", 49)),
                "v_steps": int(body.get("v_steps", 20)),
                "v_cfg": float(body.get("v_cfg", 3.0)),
                "denoise": min(1.0, max(0.1, float(body.get("denoise", 0.75)))),
                "strength": min(1.0, max(0.0, float(body.get("strength", 1.0)))),
                "negative_prompt": negative_prompt,
                "ref_width": int(body.get("ref_width") or 0),
                "ref_height": int(body.get("ref_height") or 0),
            }
            ref = body.get("ref")
            if not ref:
                ref = None
            else:
                if not isinstance(ref, dict) or ref.get("source") not in ("ours", "input", "output"):
                    raise ValueError('ref 需要形如 {"source": "ours/input/output", "name": "文件名"}')
                if not str(ref.get("name", "")).strip():
                    raise ValueError("ref.name 不能为空")
                ref = {"source": ref["source"], "name": str(ref["name"]).strip(), "subfolder": str(ref.get("subfolder", ""))}
        except Exception as e:
            self.send_json(400, {"error": f"{type(e).__name__}: {e}"})
            return
        self.send_json(200, {"id": new_job(mode, prompt, params, ref, prompt_en=prompt_en, style=style)})

    def handle_settings(self, body):
        llm = CONFIG["llm"]
        if "enabled" in body:
            llm["enabled"] = bool(body["enabled"])
        if "base_url" in body:
            llm["base_url"] = str(body["base_url"]).strip().rstrip("/")
        if "model" in body:
            llm["model"] = str(body["model"]).strip()
        if body.get("clear_key"):
            llm["api_key"] = ""
        key = str(body.get("api_key") or "").strip()
        if key and not key.startswith("****"):
            llm["api_key"] = key
        try:
            save_config()
        except Exception as e:
            self.send_json(500, {"error": f"写入 config.json 失败：{type(e).__name__}: {e}"})
            return
        self.send_json(200, llm_view())

    def handle_settings_test(self, body):
        sample = str(body.get("prompt") or "").strip() or "一只红色狐狸坐在雪地松林里，夕阳逆光，镜头缓慢推进，电影感"
        style = str(body.get("style") or "photorealistic").strip()
        if not llm_ready():
            self.send_json(200, {
                "ok": False,
                "in": sample,
                "error": "LLM 未就绪：需要填写有效 Base URL、Model，并保存 API Key",
            })
            return
        out, used, err = translate(sample, style=style)
        self.send_json(200, {"ok": not err, "in": sample, "out": out, "used": used, "error": err})


def main():
    port = int(os.environ.get("PORT", 8189))
    print(f"comfy-web  ->  http://127.0.0.1:{port}")
    print(f"ComfyUI    ->  {CONFIG['comfy_url']}")
    if llm_ready():
        state = "已启用 " + CONFIG["llm"]["model"]
    elif not CONFIG["llm"].get("enabled"):
        state = "已在页面关闭（中文按原文送进模型）"
    else:
        state = "未配置（点击右上角设置填入 API Key 与服务商地址）"
    print(f"LLM 改写   ->  {state}")
    threading.Thread(target=monitor_loop, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
