const { Client } = require('pg');
async function connect() {
  const client = new Client({ connectionTimeoutMillis: 5000 });
  await client.connect();
  await client.query('CREATE TABLE IF NOT EXISTS items (name text PRIMARY KEY, value text NOT NULL)');
  return client;
}
module.exports = { connect };
