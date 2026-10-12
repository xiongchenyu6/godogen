# 双 RTX 4090 本地 3D AI 部署手册

> 目标：部署可供 Godogen、内部工具或脚本调用的本地「图片 → PBR 3D GLB」服务。  
> 推荐主模型：Microsoft TRELLIS.2；备用模型：TripoSG。  
> 硬件：2 × RTX 4090 24GB。  
> 文档日期：2026-08-11。

## 1. 最终架构

```text
Godogen / 内部工具
        │ HTTP
        ▼
 Nginx :8080
   ├── TRELLIS.2 worker 0 :8000 ── GPU 0
   └── TRELLIS.2 worker 1 :8001 ── GPU 1
                 │
                 ▼
        /srv/ai3d/outputs/*.glb
```

推荐采用两个独立 worker，而不是让一个任务同时占用两张卡：

- RTX 4090 每张只有 24GB 显存；两张卡不会自动变成一张 48GB 卡。
- TRELLIS.2 官方要求至少 24GB 显存，单张 4090 正好达到要求。
- 双卡的主要收益是同时生成两个资产，而不是把单个资产的生成速度提高一倍。
- 每个 worker 必须只启动一个进程，不要给 Uvicorn 添加 `--workers 2`。

如果需要试验不同模型，可以停止第二个 TRELLIS worker，把 GPU 1 分配给 TripoSG。

## 2. 硬件与系统建议

推荐配置：

- Ubuntu 22.04 LTS 或 24.04 LTS，原生安装，不建议第一版放进 Docker。
- 2 × RTX 4090 24GB。
- 128GB 内存更适合双 worker；64GB 可以先从单 worker 开始测试。
- 至少 200GB 可用 SSD 空间，用于 Conda、源码、编译缓存、模型权重和生成物。
- 1600W 高质量电源，显卡分别使用独立供电线，机箱保持强制进排风。
- CUDA Toolkit 12.4；NVIDIA 驱动只需满足 CUDA 12.4 兼容要求，不必强行降级到某个旧驱动。

先检查硬件：

```bash
nvidia-smi
nvidia-smi -L
nvcc --version
free -h
df -h
```

预期 `nvidia-smi -L` 能看到两张 GPU。若没有 `nvcc`，按照 NVIDIA 官方 CUDA 12.4 安装指南安装 Toolkit，再继续后面的编译。

## 3. 创建运行用户与目录

```bash
sudo useradd --create-home --shell /bin/bash ai3d
sudo mkdir -p /opt/ai3d /srv/ai3d/cache/huggingface /srv/ai3d/outputs /srv/ai3d/versions
sudo chown -R ai3d:ai3d /opt/ai3d /srv/ai3d

sudo apt update
sudo apt install -y \
  git git-lfs curl build-essential ninja-build cmake pkg-config \
  libgl1 libglib2.0-0 libegl1 libx11-6 ffmpeg nginx jq
```

安装 Miniforge：

```bash
sudo -iu ai3d
cd /tmp
curl -L -o Miniforge3-Linux-x86_64.sh \
  https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh
bash Miniforge3-Linux-x86_64.sh -b -p /home/ai3d/miniforge3
/home/ai3d/miniforge3/bin/conda init bash
exit
```

重新进入运行用户：

```bash
sudo -iu ai3d
```

## 4. 安装 TRELLIS.2

设置编译和缓存环境：

```bash
export CUDA_HOME=/usr/local/cuda-12.4
export PATH="$CUDA_HOME/bin:$PATH"
export LD_LIBRARY_PATH="$CUDA_HOME/lib64:${LD_LIBRARY_PATH:-}"
export TORCH_CUDA_ARCH_LIST=8.9
export HF_HOME=/srv/ai3d/cache/huggingface
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
```

下载源码：

```bash
cd /opt/ai3d
git clone --recursive https://github.com/microsoft/TRELLIS.2.git
cd TRELLIS.2
git submodule update --init --recursive
git lfs install
git rev-parse HEAD | tee /srv/ai3d/versions/trellis2.commit
```

使用项目官方安装脚本创建环境：

```bash
. ./setup.sh \
  --new-env \
  --basic \
  --flash-attn \
  --nvdiffrast \
  --nvdiffrec \
  --cumesh \
  --o-voxel \
  --flexgemm
```

激活环境并增加 API 依赖：

```bash
conda activate trellis2
pip install fastapi 'uvicorn[standard]' python-multipart
```

记录最终环境，方便以后复现：

```bash
conda env export --no-builds > /srv/ai3d/versions/trellis2-conda.yml
python -c 'import torch; print(torch.__version__, torch.version.cuda); print(torch.cuda.get_device_name(0))'
```

### 安装阶段常见问题

如果 FlashAttention 或 CUDA 扩展编译失败：

1. 确认 `nvcc --version` 是 CUDA 12.4。
2. 确认 `echo "$CUDA_HOME"` 输出 `/usr/local/cuda-12.4`。
3. 确认磁盘至少剩余 50GB，编译过程会产生大量临时文件。
4. 不要同时在 Nix、系统 Python、Conda 三套环境里混装 CUDA 包。
5. 删除失败的 Conda 环境后重跑官方脚本，比逐个覆盖依赖更可靠：

```bash
conda deactivate
conda env remove -n trellis2
cd /opt/ai3d/TRELLIS.2
. ./setup.sh --new-env --basic --flash-attn --nvdiffrast --nvdiffrec --cumesh --o-voxel --flexgemm
```

## 5. 先做单卡官方测试

不要一开始就启动双 worker。先在 GPU 0 上运行项目自带示例：

```bash
sudo -iu ai3d
cd /opt/ai3d/TRELLIS.2
conda activate trellis2

export CUDA_VISIBLE_DEVICES=0
export HF_HOME=/srv/ai3d/cache/huggingface
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

python example.py
```

第一次启动会下载模型权重，时间取决于网络。测试时另开终端观察：

```bash
watch -n 1 nvidia-smi
```

成功标准：

- 只有物理 GPU 0 出现显存占用。
- 最终生成示例视频和 `.glb` 文件。
- 没有 CUDA OOM、非法指令或 CUDA 扩展加载错误。

也可以先运行官方 Gradio 界面验证人工操作：

```bash
CUDA_VISIBLE_DEVICES=0 python app.py
```

如果服务只监听本机，使用 SSH 端口转发访问：

```bash
ssh -L 7860:127.0.0.1:7860 user@server
```

## 6. 创建本地 HTTP API

在 `/opt/ai3d/TRELLIS.2/local_api.py` 创建以下文件：

```python
import os

# 必须在导入 Torch/OpenCV 相关模块前设置。
os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

import asyncio
import io
import threading
import uuid
from pathlib import Path

import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from PIL import Image

import o_voxel
from trellis2.pipelines import Trellis2ImageTo3DPipeline


OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "/srv/ai3d/outputs"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Local TRELLIS.2 API", version="1.0")
generation_lock = threading.Lock()

# CUDA_VISIBLE_DEVICES 会让每个进程只看见自己的那张卡；进程内仍然使用 cuda:0。
pipeline = Trellis2ImageTo3DPipeline.from_pretrained("microsoft/TRELLIS.2-4B")
pipeline.cuda()


def generate_glb(image: Image.Image, output_path: Path, face_limit: int, texture_size: int) -> None:
    with generation_lock, torch.inference_mode():
        mesh = pipeline.run(image)[0]

        # 官方示例中的安全上限；真正的游戏面数由下面 decimation_target 控制。
        mesh.simplify(16_777_216)

        glb = o_voxel.postprocess.to_glb(
            vertices=mesh.vertices,
            faces=mesh.faces,
            attr_volume=mesh.attrs,
            coords=mesh.coords,
            attr_layout=mesh.layout,
            voxel_size=mesh.voxel_size,
            aabb=[[-0.5, -0.5, -0.5], [0.5, 0.5, 0.5]],
            decimation_target=face_limit,
            texture_size=texture_size,
            remesh=True,
            remesh_band=1,
            remesh_project=0,
            verbose=True,
        )
        glb.export(str(output_path), extension_webp=True)
        del mesh, glb
        torch.cuda.empty_cache()


@app.get("/health")
def health():
    return {
        "status": "ok",
        "gpu": torch.cuda.get_device_name(0),
        "allocated_mb": round(torch.cuda.memory_allocated(0) / 1024 / 1024),
        "visible_devices": os.getenv("CUDA_VISIBLE_DEVICES", "all"),
    }


@app.post("/v1/image-to-3d")
async def image_to_3d(
    file: UploadFile = File(...),
    face_limit: int = Form(100_000),
    texture_size: int = Form(2048),
):
    if face_limit < 5_000 or face_limit > 1_000_000:
        raise HTTPException(400, "face_limit must be between 5000 and 1000000")
    if texture_size not in (1024, 2048, 4096):
        raise HTTPException(400, "texture_size must be 1024, 2048, or 4096")

    data = await file.read()
    if not data or len(data) > 30 * 1024 * 1024:
        raise HTTPException(400, "image is empty or larger than 30 MiB")

    try:
        image = Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception as exc:
        raise HTTPException(400, f"invalid image: {exc}") from exc

    job_id = uuid.uuid4().hex
    output_path = OUTPUT_DIR / f"{job_id}.glb"

    try:
        await asyncio.to_thread(generate_glb, image, output_path, face_limit, texture_size)
    except torch.cuda.OutOfMemoryError as exc:
        torch.cuda.empty_cache()
        raise HTTPException(507, "GPU out of memory; retry with a smaller texture or one worker") from exc

    return {
        "id": job_id,
        "status": "succeeded",
        "url": f"/v1/files/{job_id}.glb",
    }


@app.get("/v1/files/{job_id}.glb")
def download(job_id: str):
    if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
        raise HTTPException(400, "invalid job id")
    output_path = OUTPUT_DIR / f"{job_id}.glb"
    if not output_path.exists():
        raise HTTPException(404, "file not found")
    return FileResponse(output_path, media_type="model/gltf-binary", filename=output_path.name)
```

说明：

- 这是一套内部同步 API，请求会等生成完成再返回。
- 每个进程用锁保证同一张 GPU 同时只跑一个任务。
- 两个 worker 共用 `/srv/ai3d/outputs`，所以经 Nginx 负载均衡后仍能从任一 worker 下载文件。
- `face_limit=100000` 适合高质量道具预览；游戏内普通道具可先试 20,000～50,000。
- AI 自动减面不等于适合动画的角色拓扑。角色绑定前仍应检查关节附近布线。

## 7. 单 worker API 测试

```bash
sudo -iu ai3d
cd /opt/ai3d/TRELLIS.2
conda activate trellis2

export CUDA_VISIBLE_DEVICES=0
export HF_HOME=/srv/ai3d/cache/huggingface
export OUTPUT_DIR=/srv/ai3d/outputs
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

uvicorn local_api:app --host 127.0.0.1 --port 8000
```

另开终端测试：

```bash
curl http://127.0.0.1:8000/health | jq

curl -X POST http://127.0.0.1:8000/v1/image-to-3d \
  -F 'file=@test.png' \
  -F 'face_limit=50000' \
  -F 'texture_size=2048' \
  | tee response.json

curl -o result.glb \
  "http://127.0.0.1:8000$(jq -r .url response.json)"
```

用 Blender 打开 `result.glb`，检查：

- 几何是否封闭，有没有漂浮面或破面。
- Base Color、Roughness、Metallic、Opacity 是否正确导入。
- 面数和贴图分辨率是否符合目标平台。
- 法线方向、模型原点和比例是否需要后处理。

## 8. 配置双 GPU systemd 服务

创建 `/etc/systemd/system/trellis2@.service`：

```ini
[Unit]
Description=TRELLIS.2 worker on physical GPU %i
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ai3d
Group=ai3d
WorkingDirectory=/opt/ai3d/TRELLIS.2

Environment=CUDA_VISIBLE_DEVICES=%i
Environment=CUDA_HOME=/usr/local/cuda-12.4
Environment=HF_HOME=/srv/ai3d/cache/huggingface
Environment=OUTPUT_DIR=/srv/ai3d/outputs
Environment=PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
Environment=PATH=/home/ai3d/miniforge3/envs/trellis2/bin:/usr/local/cuda-12.4/bin:/usr/bin:/bin
Environment=LD_LIBRARY_PATH=/usr/local/cuda-12.4/lib64

ExecStart=/home/ai3d/miniforge3/envs/trellis2/bin/uvicorn local_api:app --host 127.0.0.1 --port 800%i
Restart=on-failure
RestartSec=10
TimeoutStartSec=900
TimeoutStopSec=60
KillSignal=SIGINT

LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
```

先启动 GPU 0：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now trellis2@0
sudo journalctl -u trellis2@0 -f
```

确认成功后，再启动 GPU 1：

```bash
sudo systemctl enable --now trellis2@1
sudo journalctl -u trellis2@1 -f
```

检查两张卡：

```bash
curl http://127.0.0.1:8000/health | jq
curl http://127.0.0.1:8001/health | jq
nvidia-smi
```

两个 health 响应中的 `visible_devices` 应分别是 `0` 和 `1`。

## 9. 配置 Nginx 负载均衡

创建 `/etc/nginx/sites-available/ai3d`：

```nginx
upstream trellis2_workers {
    least_conn;
    server 127.0.0.1:8000 max_fails=2 fail_timeout=30s;
    server 127.0.0.1:8001 max_fails=2 fail_timeout=30s;
}

server {
    listen 8080;
    server_name _;

    client_max_body_size 32m;

    location / {
        proxy_pass http://trellis2_workers;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_connect_timeout 30s;
        proxy_send_timeout 1800s;
        proxy_read_timeout 1800s;
    }
}
```

启用配置：

```bash
sudo ln -s /etc/nginx/sites-available/ai3d /etc/nginx/sites-enabled/ai3d
sudo nginx -t
sudo systemctl reload nginx
curl http://127.0.0.1:8080/health | jq
```

安全建议：

- 默认只在内网或 VPN 中开放 8080，不要直接暴露到公网。
- 如需公网访问，在 Nginx 前增加 TLS、OIDC 或至少 Bearer Token 校验。
- 使用防火墙限制调用来源，例如只允许 Godogen 主机访问。
- 定期清理 `/srv/ai3d/outputs`，避免生成物无限增长。

例如每天删除 30 天前的输出：

```bash
sudo crontab -e
```

加入：

```cron
20 4 * * * find /srv/ai3d/outputs -type f -name '*.glb' -mtime +30 -delete
```

## 10. Godogen 接入

这套 API 不是 Tripo API 的完全兼容实现。建议给 Godogen 增加一个 `local_trellis` provider，而不是假装成 Tripo 任务接口。

调用流程：

```text
Godogen 先生成参考图
        ↓
POST /v1/image-to-3d
        ↓
获得 { status, url }
        ↓
GET url 下载 GLB
        ↓
导入 Godot / Bevy / Babylon.js 或进入 Blender 后处理
```

请求示例：

```bash
curl -X POST http://ai3d-server:8080/v1/image-to-3d \
  -F 'file=@concept.png' \
  -F 'face_limit=30000' \
  -F 'texture_size=2048'
```

Godogen provider 只需要完成三件事：

1. 将参考图片用 multipart 的 `file` 字段上传。
2. 读取响应中的 `url`。
3. 将下载的 GLB 保存到项目资产目录，并触发引擎重新导入。

建议按资产类型设置默认参数：

| 资产类型 | `face_limit` | `texture_size` |
|---|---:|---:|
| 小型场景道具 | 10,000～25,000 | 1024 |
| 主要道具/武器 | 25,000～60,000 | 2048 |
| 主角原型 | 60,000～120,000 | 2048～4096 |
| 远景装饰 | 5,000～15,000 | 1024 |

对移动端和 WebGPU 项目，应额外生成 LOD，而不是只使用一份高模。

## 11. 可选：在 GPU 1 部署 TripoSG

TripoSG 是 MIT 许可的开源图片转 Mesh 模型，显存要求低于 TRELLIS.2，但它不是 Tripo H3.1/P1，纹理和成品化能力也较弱。适合作为：

- 快速几何原型。
- TRELLIS.2 OOM 时的备用路径。
- 无纹理碰撞体或远景模型生成器。

先释放 GPU 1：

```bash
sudo systemctl disable --now trellis2@1
```

安装：

```bash
sudo -iu ai3d
cd /opt/ai3d
git clone https://github.com/VAST-AI-Research/TripoSG.git
cd TripoSG

/home/ai3d/miniforge3/bin/conda create -y -n triposg python=3.10
conda activate triposg

pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124
pip install -r requirements.txt
git rev-parse HEAD | tee /srv/ai3d/versions/triposg.commit
```

测试：

```bash
export CUDA_VISIBLE_DEVICES=1
python -m scripts.inference_triposg \
  --image-input assets/example_data/hjswed.png \
  --faces 50000 \
  --output-path /srv/ai3d/outputs/triposg-test.glb
```

注意：设置 `CUDA_VISIBLE_DEVICES=1` 后，该进程内部看到的仍然是 `cuda:0`，这是正常现象。

## 12. 监控与运维

实时查看 GPU：

```bash
watch -n 1 'nvidia-smi --query-gpu=index,name,temperature.gpu,power.draw,memory.used,memory.total,utilization.gpu --format=csv'
```

查看服务日志：

```bash
sudo journalctl -u trellis2@0 -f
sudo journalctl -u trellis2@1 -f
```

查看输出空间：

```bash
du -sh /srv/ai3d/outputs /srv/ai3d/cache/huggingface
find /srv/ai3d/outputs -type f -name '*.glb' | wc -l
```

升级前先记录当前版本：

```bash
cd /opt/ai3d/TRELLIS.2
git rev-parse HEAD
conda list --explicit > /srv/ai3d/versions/trellis2-explicit.txt
```

不要在生产环境直接执行无人值守的 `git pull`。先停止一个 worker，在该 worker 上验证新版本，成功后再滚动升级第二个。

## 13. OOM 与故障排查

### 启动时就 OOM

- 检查该 GPU 是否已有桌面、LLM、ComfyUI 或其他进程占用显存。
- 使用 `nvidia-smi` 找出占用者。
- 先只运行 `trellis2@0`，不要同时启动两个服务。
- 确认每个 systemd 实例都设置了不同的 `CUDA_VISIBLE_DEVICES`。

### 第一单成功，第二单 OOM

- 确认 API 在任务结束后执行了 `del` 和 `torch.cuda.empty_cache()`。
- 降低贴图为 1024 或 2048。
- 降低输出目标面数。
- 重启对应 worker：

```bash
sudo systemctl restart trellis2@0
```

### 两个请求都打到同一张卡

- 确认 Nginx 使用 `least_conn`。
- 确认 8000、8001 两个 health endpoint 都正常。
- 确认没有使用单个 Uvicorn 进程配多个线程绕过 GPU 锁。

### 生成结果不适合游戏

生成模型解决的是初始资产，不保证最终生产质量。常见后处理包括：

- Blender 中修复非流形面、法线和悬空碎片。
- 为动画角色重新拓扑，而不是直接绑定自动减面网格。
- 重新 UV 或压缩贴图。
- 生成 LOD0/LOD1/LOD2 和独立碰撞网格。
- 在 Godot/Bevy/Babylon.js 中检查坐标轴、单位和 PBR 通道。

## 14. 推荐运行模式

### 模式 A：批量生产，推荐

```text
GPU 0 = TRELLIS.2 worker 0
GPU 1 = TRELLIS.2 worker 1
```

适合大量生成游戏道具，吞吐量最高。

### 模式 B：开发与对比

```text
GPU 0 = TRELLIS.2
GPU 1 = TripoSG / 其他开源模型
```

适合比较质量、速度和稳定性。

### 模式 C：商业 API 兜底

```text
本地 TRELLIS.2 优先
        ↓ 失败或质量不达标
Tripo API / Scenario 兜底
```

这是最实用的成本结构：普通资产走本地，角色绑定、复杂分件和少数高要求资产走商业 API。

## 15. 上线检查清单

- [ ] `nvidia-smi -L` 能看到两张 RTX 4090。
- [ ] CUDA Toolkit 12.4 和编译器可用。
- [ ] 官方 `example.py` 在单张 GPU 上成功。
- [ ] API 单 worker 成功生成并下载 GLB。
- [ ] Blender 能正确打开 GLB 和 PBR 材质。
- [ ] `trellis2@0` 和 `trellis2@1` 分别占用不同 GPU。
- [ ] Nginx 能把并发请求分配到两个 worker。
- [ ] API 只在内网/VPN 或受认证的入口开放。
- [ ] 输出目录有清理策略。
- [ ] Godogen provider 能上传图片、读取 URL、下载 GLB。
- [ ] 为商业 API 配置兜底策略。

## 16. 官方资料

- TRELLIS.2：https://github.com/microsoft/TRELLIS.2
- TripoSG：https://github.com/VAST-AI-Research/TripoSG
- TripoSR：https://github.com/VAST-AI-Research/TripoSR
- NVIDIA CUDA 安装指南：https://docs.nvidia.com/cuda/cuda-installation-guide-linux/

TRELLIS.2 的模型与主体代码使用 MIT License；TripoSG 和 TripoSR 也使用 MIT License。仍需确保输入图片、训练素材、生成角色及品牌元素不存在第三方版权或商标问题。
