import { defineConfig } from "vite";
import { viteStaticCopy } from "vite-plugin-static-copy";

// Cesium ships workers, assets and widgets that must be served as static files.
const cesiumSource = "node_modules/cesium/Build/Cesium";
const cesiumBaseUrl = "cesium";

export default defineConfig({
  define: {
    CESIUM_BASE_URL: JSON.stringify(`/${cesiumBaseUrl}`),
  },
  plugins: [
    viteStaticCopy({
      targets: [
        {
          src: `${cesiumSource}/{ThirdParty,Workers,Assets,Widgets}/**/*`,
          dest: cesiumBaseUrl,
          // keep Workers/..., Assets/... but drop the node_modules/cesium/Build/Cesium prefix
          rename: { stripBase: 4 },
        },
      ],
    }),
  ],
  server: {
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
});
