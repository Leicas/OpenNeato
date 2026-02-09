import { defineConfig } from "vite";
import preact from "@preact/preset-vite";

export default defineConfig({
    plugins: [preact()],
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
});
