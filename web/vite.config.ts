import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

/**
 * Vite 配置
 *
 * ★ 两个关键设置：
 *
 * 1. `base: "/static/app/"` —— 构建产物会被 Starlette 挂在 /static/app/ 下。
 *    不用根路径，是为了能和旧的 static/index.html **并存**一段时间，
 *    方便用截图工具对比新旧界面；确认无误后再切换。
 *
 * 2. dev 时代的 proxy —— 开发时前端跑在 5173、接口在 8600，
 *    必须把 /api 转发过去，否则 fetch 会 404。
 *    这就是"热更新 + 真实后端"能同时成立的原因。
 */
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: "/static/app/",
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8600",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
