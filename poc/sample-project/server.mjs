import { createServer } from 'node:http';

export function greeting(name = 'world') {
  return { message: `hello ${name}` };
}

if (process.argv[1]?.endsWith('server.mjs')) {
  const server = createServer((request, response) => {
    const url = new URL(request.url, 'http://localhost');
    response.writeHead(200, { 'content-type': 'application/json' });
    response.end(JSON.stringify(greeting(url.searchParams.get('name') ?? 'world')));
  });
  server.listen(Number(process.env.PORT ?? 8080), '0.0.0.0');
}

