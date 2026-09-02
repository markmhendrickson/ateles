import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";
import { neotomaProxy } from "./server/neotomaProxy";

export default defineConfig({
  plugins: [react(), neotomaProxy()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: { port: 5273, strictPort: false },
});
