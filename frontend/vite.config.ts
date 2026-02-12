import { defineConfig } from "vite";
import preact from "@preact/preset-vite";

export default defineConfig(({ command }) => ({
    plugins: [
        preact(),
        ...(command === "serve"
            ? [require("./mock/server.js").mockApiPlugin()]
            : []),
    ],
    build: {
        outDir: "dist",
        assetsInlineLimit: 0,
        rollupOptions: {
            output: {
                entryFileNames: "app.js",
                assetFileNames: "[name][extname]",
            },
        },
    },
}));
