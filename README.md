# ComfyUI-LLM-Lite 插件

轻量 ComfyUI 插件：调用 **OpenAI 兼容** `v1/chat/completions` 输出文本。模型列表通过节点上的**按钮**获取（不点 Run、不入队），获取后直接在**下拉框**中选择模型。支持 **本地 llama.cpp** 与云端 OpenAI 兼容服务。

## 功能

- 调用 OpenAI 兼容 `v1/chat/completions` API
- 支持 system prompt 和 user prompt（多行输入）
- 支持自定义 API 地址、API 密钥（masked 格式）
- 模型为**下拉框**选择，点击模型上方的「获取模型列表」按钮动态填充（无需运行工作流）
- 支持采样参数：temperature、top_p、top_k、seed
- 支持思考开关（enable_thinking）与自定义 max_tokens
- **seed + control_after_generate**（同官方 RandomNoise）：随机/固定/递增/递减；`randomize` 每次入队换 seed，绕过 ComfyUI 输出缓存并重新请求
- 发送前自动检测 llama-server 路由模型状态，未加载时调用 `/models/load`；遇 `proxy error` 自动重载并重试一次
- 流式请求；ComfyUI「取消当前任务」时关闭 HTTP 连接，llama-server 随之停止生成；若启用 unload_model，取消后仍会请求 `/models/unload`
- 支持请求超时（timeout，默认 300 秒）
- 支持卸载模型（unload_model）：通过 llamacpp `POST /models/unload` 卸载本地模型
- 多模态图片：最多 10 张参考图，以 OpenAI `image_url`（base64）发送

## 安装

将本目录复制到 ComfyUI 的 `custom_nodes/` 下（目录名建议 `ComfyUI-LLM-Lite`），重启 ComfyUI，浏览器硬刷新（`Ctrl+Shift+R`）加载前端脚本。

![API](https://raw.githubusercontent.com/binfen23/ComfyUI-LLM-Lite/refs/heads/main/img/API.png)  

![本地 llamacpp](https://github.com/binfen23/ComfyUI-LLM-Lite/blob/main/img/local.png?raw=true)



## 本地 llama.cpp 使用说明

本地推理**必须**以 **Router Mode** 启动 `llama-server`（llama.cpp b10883+），否则本插件的模型列表、`/models/load`、`/models/unload`、`proxy error` 自动重载均不可用。

### 1. 启动时必须指定模型路径

Router 模式下需要把模型目录交给服务端扫描/注册，例如：

```bat
@echo off
set "MODEL_DIR=%~dp0"

REM 启动 llama.cpp Router Mode

".\llama-server.exe" ^
  --models-dir "%MODEL_DIR%models" ^
  --host 0.0.0.0 ^
  --port 8080 ^
  -ngl 99 ^
  -t 8 ^
  --jinja ^
  -c 80000 ^
  --batch-size 4096 ^
  --ubatch-size 2048 ^
  --cache-type-k q4_0 ^
  --cache-type-v q4_0 ^
  -fa on ^
  --reasoning-budget 3072 ^
  --temp 0.6 ^
  --top-p 0.95 ^
  --top-k 20
```


### 2. 多模态 / mmproj：主模型与 mmproj 必须同目录

若要用图片输入（vision），**mmproj 不能与主模型散落在 `models/` 根目录**。Router 只在「模型子文件夹」内自动配对 `path_mmproj`：

```
{llama.cpp}/models/
├── model-1.gguf
└── Your-Vision-Model/                 ← 新建文件夹
    ├── model-2.gguf                   ← 主模型
    └── *.mmproj-bf16.gguf             ← 对应 mmproj（同文件夹）
```

要点：

- 新建**一个文件夹**，把**主模型**和 **mmproj** 都放进去
- 文件名需能被 Router 识别为同一套模型（通常 `xxx.gguf` + `xxx.mmproj-*.gguf`）
- 若 mmproj 放在 `models/` 根目录且主模型也是平铺，Router 可能得到空的 `path_mmproj`，图片请求会报 `image input is not supported - hint: if this is unexpected, you may need to provide the mmproj`
- 启动后点「获取模型列表」；需要图片时选中该视觉模型，节点会按需 `/models/load`

### 3. 云端服务

`api.openai.com` 或其它 OpenAI 兼容网关：填写 Base URL + Key 即可，无需 Router，也不会走本地 load/unload。

## 使用

1. 添加 **LLM Chat** 节点（Node ID：`LLMChat`，分类 `text/LLM`）
2. 填写 **API URL** 和 **API Key**
3. 点击 **model 上方**的 **「获取模型列表」** 按钮（不需要点 Run）
4. 在 **model** 下拉框中选择模型
5. 填写 prompt（可连图），点 Run 运行

### 参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| **api_url** | OpenAI 兼容地址（`.../v1` 或 `.../v1/chat/completions`） | `http://127.0.0.1:8080/v1` |
| **api_key** | API 密钥（Bearer token） | `sk-********`（需填写） |
| **system_prompt** | 系统提示词 | `You are a helpful assistant.` |
| **user_prompt** | 用户输入提示词 | （留空） |
| **images**（动态端口） | 参考图片，连一张自动多一个端口，最多 10 张；按顺序以 OpenAI `image_url`（base64 data URL）发送，对应 `<image1>`、`<image2>`… | 无 |
| **model** | 模型下拉框（点按钮获取列表后可选） | （空） |
| **seed** | 采样种子（0 ~ 2⁶⁴-1）+ 运行后控制：固定/递增/递减/随机 | `0` / 随机 |
| **temperature** | 采样温度（0.0 - 2.0） | `1.0` |
| **top_p** | Nucleus 采样（0.0 - 1.0） | `0.95` |
| **top_k** | Top-k 采样（1 - 100） | `20` |
| **enable_thinking** | 思考/推理开关：开=发 `chat_template_kwargs.enable_thinking=true`；关=再发 `reasoning_budget_tokens=0` 双保险 | 开启 |
| **max_tokens** | 最大生成 token 数（1 - 32768） | `4096` |
| **timeout** | 请求超时秒数（1 - 3600） | `300` |
| **unload_model** | 调用后通过 llamacpp 卸载本地模型 | 关闭 |

### 输出

| 输出 | 说明 |
|------|------|
| **generated_text** | API 返回的文本内容 |

## 模型列表获取（按钮）

- 按钮位于 **model 下拉框正上方**
- 前端调用自定义路由 `POST /llm_lite/fetch_models`，由后端以 **GET** 请求 `{base_url}/v1/models`
- 仅需 API URL 和 API Key，**不需要指定模型、不需要点 Run**
- 成功后模型 ID 自动填入 model 下拉框；失败会弹出错误信息
- 请求格式：

  ```
  POST /llm_lite/fetch_models
  Content-Type: application/json

  {"api_url": "...", "api_key": "..."}
  ```

  响应：

  ```json
  {"models": ["model-id-1", "model-id-2"], "error": null}
  ```

## API URL 填写

两种格式均可，自动归一化：

| 填写示例 | chat 请求 | 模型列表请求 |
|----------|-----------|--------------|
| `http://127.0.0.1:8080/v1`（默认） | 自动补 `/chat/completions` | `GET http://127.0.0.1:8080/v1/models` |
| `http://127.0.0.1:8080/v1/chat/completions` | 原样 POST | `GET http://127.0.0.1:8080/v1/models` |
| `https://api.openai.com/v1` | 自动补 `/chat/completions` | `GET https://api.openai.com/v1/models` |

## API 格式

**请求（纯文本）：**

```
POST {chat_url}

Headers:
  Authorization: Bearer {api_key}
  Content-Type: application/json

Body:
{
  "model": "your-local-model-id",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "你好"}
  ],
  "temperature": 1.0,
  "top_p": 0.95,
  "top_k": 20,
  "seed": 0,
  "max_tokens": 4096,
  "chat_template_kwargs": {"enable_thinking": true}
}
```

接了参考图时，messages 切换为多模态 content 数组格式（llama.cpp OpenAI 兼容）：

```json
{
  "model": "your-vision-model-id",
  "messages": [
    {"role": "system", "content": [{"type": "text", "text": "You are a helpful assistant."}]},
    {"role": "user", "content": [
      {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
      {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
      {"type": "text", "text": "Place <image1>'s subject into <image2>'s scene"}
    ]}
  ]
}
```

从 `response["choices"][0]["message"]["content"]` 提取文本；若 `content` 为空则回退 `reasoning_content`（推理模型正文可能在思考字段）。

## 输出缓存与重新请求

ComfyUI 对相同输入会缓存节点输出，重复点 Run 若输入指纹不变会在 ~0.02s 直接回放旧结果，不会打到 API。

本节点带 **seed + 运行后控制**（与官方 RandomNoise 相同）：

| 运行后控制 | 行为 |
|------------|------|
| **随机 randomize** | 每次入队生成新 seed → 输入变化 → 缓存失效 → 重新请求 API |
| **固定 fixed** | seed 不变；其它输入也不变时走缓存 |
| **递增/递减** | seed ±1，同样会触发重新请求 |

seed 会随请求体发给 llama-server（OpenAI 兼容 `seed` 字段），固定 seed + temperature=0 可尽量复现结果。

也可在 workflow 里外接 **RandomNoise** 节点到 `seed`，用它的运行后控制驱动本节点。

## 卸载模型

开启 **unload_model** 后，节点在成功输出文本（或取消/请求异常）时调用：

```
POST {base_url}/models/unload
Body: {"model": "<当前模型>"}
```

卸载失败仅记录 warning，不影响节点执行。第三方云端 API 通常无此路由，建议关闭。

## 依赖

- Python 3.13+
- `requests`（ComfyUI 自带）
- `aiohttp`（ComfyUI 自带）
- `comfy_api.latest`（ComfyUI 自带）

## 节点信息

- **Node ID**: `LLMChat`
- **Display Name**: `LLM Chat`
- **Category**: `text/LLM`
- **插件名**: `ComfyUI-LLM-Lite`
- **前端控件**: model 上方「获取模型列表」按钮（点击即拉取，不入队）

## 目录结构

```
ComfyUI-LLM-Lite/
├── __init__.py              # 包初始化，导出 WEB_DIRECTORY 与 comfy_entrypoint
├── nodes.py                 # 节点定义 + /llm_lite/fetch_models 路由
├── web/
│   └── js/
│       └── fetch_models.js  # 前端按钮（插在 model 上方）+ 动态填充下拉框
└── README.md
```
