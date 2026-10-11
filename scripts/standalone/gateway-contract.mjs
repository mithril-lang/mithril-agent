import {createHash,createHmac,timingSafeEqual} from 'node:crypto';

export const profile = Object.freeze({
  schema: 1, name: 'gateway-docker-linux-amd64', platform: 'linux/amd64',
  coverage: ['repository Dockerfile', 'offline API gateway regression suite', 'real image provenance, SQLite, privilege drop and restart'],
  excluded: ['native Desktop and installers', 'messaging adapter delivery', 'other architectures', 'live inference', 'production publication'],
  minimumFreeBytes: 12 * 1024 ** 3,
  offlineTests: ['tests/hermes_cli/test_profile_handoff.py', 'tests/tui_gateway/test_profile_handoff.py', 'tests/tui_gateway/contracts/test_generated.py', 'tests/gateway/test_api_server*.py', 'tests/gateway/test_multiplex_api_server_routing.py', 'tests/gateway/test_config.py', 'tests/gateway/test_custom_provider_request_overrides.py', 'tests/hermes_cli/test_container_boot.py', 'tests/hermes_cli/test_gateway_external_supervisor.py'],
});
export const digest = value => createHash('sha256').update(typeof value === 'string' ? value : JSON.stringify(value)).digest('hex');
export function seal(receipt, key) {
  return {receipt, signature: createHmac('sha256', key).update(JSON.stringify(receipt)).digest('hex')};
}
export function admit(envelope, key, expected, now = Date.now()) {
  const signature = seal(envelope.receipt, key).signature;
  if (!/^[a-f0-9]{64}$/.test(envelope.signature ?? '') || !timingSafeEqual(Buffer.from(signature), Buffer.from(envelope.signature))) throw Error('Invalid receipt signature');
  const r = envelope.receipt;
  for (const [name,value] of Object.entries(expected)) if (r[name] !== value) throw Error(`Receipt ${name} mismatch`);
  const age = now - Date.parse(r.finishedAt);
  if (r.status !== 'success' || !Number.isFinite(age) || age < 0 || age > 86400000 || !/^sha256:[a-f0-9]{64}$/.test(r.imageId ?? '')) throw Error('Successful fresh image qualification required');
  if (r.productionEligible !== false) throw Error('Gateway trial qualification cannot authorize a production release');
  for (const check of ['image-build','offline-regressions','image-runtime']) {
    if (!Array.isArray(r.checks) || !r.checks.includes(check)) throw Error(`Missing qualification stage: ${check}`);
  }
  return r;
}
export function validateOwner(owner, host, checkout) {
  if (owner.repository !== 'mithril-lang/mithril-agent' || owner.hostname !== host || owner.checkout !== checkout) throw Error('Deployment owner mismatch');
  if (!['local','gad'].includes(owner.runner)) throw Error('Only configured local/gad runners are supported');
  if (owner.socket && !/^\/run\/mithril-agent-trial\/docker\.sock$/.test(owner.socket)) throw Error('Unexpected runner socket');
  return owner;
}
