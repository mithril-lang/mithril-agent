import {test} from 'node:test';
import assert from 'node:assert/strict';
import {randomBytes} from 'node:crypto';
import {seal,admit,validateOwner} from '../../../scripts/standalone/gateway-contract.mjs';

test('admission binds success, source, recipe, image, owner and freshness', () => {
  const key=randomBytes(32), now=Date.now();
  const expected={repository:'mithril-lang/mithril-agent',sha:'a'.repeat(40),recipeDigest:'b'.repeat(64),ownerIdentity:'owner'};
  const r={...expected,status:'success',finishedAt:new Date(now).toISOString(),imageId:'sha256:'+'c'.repeat(64),checks:['image-build','offline-regressions','image-runtime'],productionEligible:false};
  assert.equal(admit(seal(r,key),key,expected,now).imageId,r.imageId);
  assert.throws(()=>admit({...seal(r,key),receipt:{...r,sha:'d'.repeat(40)}},key,expected,now),/signature/);
  for (const patch of [{status:'failed'}, {imageId:null}, {checks:[]}, {checks:['image-build','image-runtime']}, {productionEligible:true}, {finishedAt:new Date(now-86400001).toISOString()}]) assert.throws(()=>admit(seal({...r,...patch},key),key,expected,now));
  assert.throws(()=>admit(seal(r,key),key,{...expected,ownerIdentity:'other'},now),/mismatch/);
});

test('owner is the configured Mac checkout and a fixed verification runner', () => {
  const o={repository:'mithril-lang/mithril-agent',hostname:'mac',checkout:'/checkout',runner:'gad',socket:'/run/mithril-agent-trial/docker.sock'};
  assert.equal(validateOwner(o,'mac','/checkout'),o);
  for (const patch of [{hostname:'other'},{checkout:'/other'},{runner:'untrusted'},{socket:'/var/run/docker.sock'}]) assert.throws(()=>validateOwner({...o,...patch},'mac','/checkout'));
});
