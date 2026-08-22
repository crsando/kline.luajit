import { APP_CONFIG } from "./config.js";
import { buildApp } from "./api.js";

const app = buildApp();

try {
  await app.listen({ host: APP_CONFIG.host, port: APP_CONFIG.port });
  console.log(`Market Profile Dashboard: http://${APP_CONFIG.host}:${APP_CONFIG.port}`);
  console.log(`Tick data root: ${APP_CONFIG.dataRoot}`);
} catch (error) {
  app.log.error(error);
  process.exitCode = 1;
}

for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.on(signal, async () => {
    await app.close();
    process.exit(0);
  });
}
