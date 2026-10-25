const http = require('node:http');
const { connect } = require('./dataset.cjs');

function createServer() {
  return http.createServer(async (req, res) => {
    if (req.method !== 'GET' || req.url !== '/health') {
      res.writeHead(404).end();
      return;
    }
    let client;
    try {
      client = await connect();
      const result = await client.query('SELECT count(*)::int AS count FROM items');
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ healthy: true, items: result.rows[0].count }));
    } catch {
      console.error('Sample database check failed');
      res.writeHead(503).end('Database unavailable');
    } finally {
      if (client) await client.end();
    }
  });
}

if (require.main === module) {
  const server = createServer();
  server.listen(Number(process.env.PORT || 3000), '0.0.0.0', () => console.log('Sample application started'));
  process.on('SIGTERM', () => server.close(() => process.exit(0)));
}
module.exports = { createServer };
