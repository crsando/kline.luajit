import { buildApp } from "./app.js";
import { loadConfig } from "./config.js";

const config = loadConfig();
const app = buildApp(config);

try {
  await app.listen({ host: config.host, port: config.port });
  console.log(`Market Profile Research: http://${config.host}:${config.port}`);
  console.log(`Data root: ${config.dataRoot}`);
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
