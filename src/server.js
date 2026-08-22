import { createServer } from 'node:http';
import { route } from './http/router.js';

const port = Number(process.env.PORT ?? 3000);

const server = createServer((request, response) => {
  route(request, response).catch((error) => {
    response.writeHead(500, { 'Content-Type': 'application/json; charset=utf-8' });
    response.end(JSON.stringify({ error: 'internal server error', detail: error.message }));
  });
});

server.listen(port, () => {
  console.log(`onAIr backend listening on http://localhost:${port}`);
});
