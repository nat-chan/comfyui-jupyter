import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const TAG = "[comfyui_jupyter]";

// api.queuePrompt をインターセプトして prompt_id を捕まえる
let lastPromptId = null;
let lastError = null;
const _originalQueuePrompt = api.queuePrompt;
api.queuePrompt = async function (...args) {
    lastPromptId = null;
    lastError = null;
    try {
        const res = await _originalQueuePrompt.apply(api, args);
        lastPromptId = res?.prompt_id ?? null;
        console.log(TAG, "api.queuePrompt intercepted, prompt_id:", lastPromptId);
        return res;
    } catch (e) {
        // /prompt が 400 を返すと { response: { error: {...}, node_errors: {...} } } が throw される
        lastError = e?.response ?? { error: String(e) };
        console.error(TAG, "api.queuePrompt error:", lastError);
        throw e;
    }
};

api.addEventListener("comfyui_jupyter/queue_prompt", async ({ detail }) => {
    const requestId = detail.request_id;
    console.log(TAG, "received queue_prompt", { requestId });

    try {
        await app.queuePrompt(0);
    } catch (e) {
        console.error(TAG, "queuePrompt failed", e);
    }

    const result = { request_id: requestId, prompt_id: lastPromptId };
    if (lastError != null) {
        result.error = lastError;
    }

    console.log(TAG, "reporting result:", result);
    await fetch("/comfyui_jupyter/queue_prompt_result", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(result),
    });
});

app.registerExtension({
    name: "jupyter.trigger_queue",
});
