let app, api;

if (window?.comfyAPI?.app?.app && window?.comfyAPI?.api?.api) {
    app = window.comfyAPI.app.app;
    api = window.comfyAPI.api.api;
} else {
    ({ app } = await import("../../../scripts/app.js"));
    ({ api } = await import("../../../scripts/api.js"));
}

function addFetchButton(node) {
    if (node.__llmLiteBtn) return true;

    const apiUrlW = node.widgets?.find((w) => w.name === "api_url");
    const apiKeyW = node.widgets?.find((w) => w.name === "api_key");
    const modelW = node.widgets?.find((w) => w.name === "model");
    if (!modelW) return false;

    node.__llmLiteBtn = true;
    modelW.options = modelW.options || {};
    modelW.options.values = modelW.options.values || [];

    // Graph save: absolute index, skips only widget.serialize === false.
    // Graph restore: sequential over serialize !== false.
    // Mid-array widget.serialize === false leaves a JSON null hole → later
    // values shift (NaN / 参数错位). Leave widget.serialize undefined so the
    // button occupies its slot. API prompt skips options.serialize === false.
    const btn = node.addWidget(
        "button",
        "获取模型列表",
        null,
        async () => {
            const api_url = apiUrlW?.value ?? "";
            const api_key = apiKeyW?.value ?? "";
            if (!api_url) {
                alert("请先填写 API URL");
                return;
            }
            try {
                const resp = await api.fetchApi("/llm_lite/fetch_models", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ api_url, api_key }),
                });
                const data = await resp.json();
                if (data.error) {
                    alert("获取失败: " + data.error);
                    return;
                }
                const models = data.models || [];
                if (!models.length) {
                    alert("无可用模型");
                    return;
                }
                modelW.options.values = models;
                if (!models.includes(modelW.value)) {
                    modelW.value = models[0];
                }
                node.setDirtyCanvas(true, true);
            } catch (e) {
                alert("请求失败: " + e.message);
            }
        },
        { serialize: false, canvasOnly: true }
    );
    // options.serialize=false keeps the button out of the API prompt body.
    // Do NOT set btn.serialize = false — graph save uses absolute index and
    // skips only widget.serialize === false, which leaves a null hole mid-array
    // and shifts every later widget on restore (NaN / 参数错位).
    delete btn.serialize;

    const widgets = node.widgets;
    const btnIdx = widgets.indexOf(btn);
    const modelIdx = widgets.indexOf(modelW);
    if (modelIdx >= 0 && btnIdx > modelIdx) {
        widgets.splice(btnIdx, 1);
        widgets.splice(modelIdx, 0, btn);
        node._widgetSlotsDirty = true;
        node.expandToFitContent?.();
        node.setDirtyCanvas(true, true);
    }
    return true;
}

console.log("[LLM-Lite] loaded");

app.registerExtension({
    name: "ComfyUI-LLM-Lite.fetchModels",
    async nodeCreated(node) {
        if (node.comfyClass !== "LLMChat" && node.type !== "LLMChat") return;
        if (addFetchButton(node)) return;
        let tries = 0;
        const timer = setInterval(() => {
            if (addFetchButton(node) || ++tries > 50) clearInterval(timer);
        }, 50);
    },
});
