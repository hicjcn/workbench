const test = require('node:test');
const assert = require('node:assert/strict');
const { connect } = require('./dataset.cjs');
test('PostgreSQL write and read in the current requirement', async () => {
  const client = await connect();
  try {
    await client.query("INSERT INTO items VALUES ('sample', 'verified') ON CONFLICT (name) DO UPDATE SET value=excluded.value");
    const result = await client.query("SELECT value FROM items WHERE name='sample'");
    assert.equal(result.rows[0].value, 'verified');
  } finally { await client.end(); }
});
