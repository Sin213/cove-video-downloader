'use strict';
/**
 * GHSA-7g7r-gx96-252g: keep the vulnerable Go AppRun generator unreachable.
 * Check declarations, every locked builder copy, and the installed toolchain
 * without loading builder internals or executing Electron / an AppImage.
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const ADVISORY = 'GHSA-7g7r-gx96-252g';
const FLOOR = [26, 15, 0];
const EXACT = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/;
const LOCKED_BUILDERS = /(?:^|\/)node_modules\/(electron-builder|app-builder-lib|builder-util|dmg-builder)$/;
const GO_BUILDER = /(?:^|\/)node_modules\/app-builder-bin$/;

// Read-only checker: the root is explicit so fixture checks cannot read the repo.
function checkAppImageBuildSecurity(root) {
  const errors = [];
  const fail = message => errors.push(`${ADVISORY}: ${message}`);
  function read(relative) {
    try {
      return JSON.parse(fs.readFileSync(path.join(root, ...relative.split('/')), 'utf8'));
    } catch (error) {
      fail(`${relative} cannot be read: ${error.message}`);
      return {};
    }
  }
  function checkVersion(version, label) {
    if (typeof version !== 'string' || !EXACT.test(version)) {
      fail(`${label} must be an exact x.y.z version; got ${JSON.stringify(version)}`);
      return;
    }
    const parts = version.split('.').map(Number);
    const different = parts.findIndex((part, index) => part !== FLOOR[index]);
    if (different !== -1 && parts[different] < FLOOR[different]) {
      fail(`${label} ${version} is below fixed version 26.15.0`);
    }
  }

  const manifest = read('package.json');
  const lock = read('package-lock.json');
  const spec = manifest.devDependencies?.['electron-builder'];
  checkVersion(spec, 'package.json electron-builder');
  // This static configuration gate does not prove runtime sandboxing;
  // runtime proof is the extracted-AppImage evidence.
  for (const section of ['appImage', 'linux']) {
    const args = manifest.build?.[section]?.executableArgs;
    if (section === 'linux' && args === undefined) continue;
    if (!Array.isArray(args) || args.length !== 0) {
      const value = args === undefined ? '<absent>' : JSON.stringify(args);
      fail(`package.json build.${section}.executableArgs must be an empty Array; got ${value}`);
    }
  }
  const packages = lock.packages || {};
  if (packages['']?.devDependencies?.['electron-builder'] !== spec) {
    fail('lock root electron-builder spec must match package.json');
  }
  for (const name of ['electron-builder', 'app-builder-lib']) {
    if (!packages[`node_modules/${name}`]) fail(`lock is missing node_modules/${name}`);
  }
  for (const [location, entry] of Object.entries(packages)) {
    const match = location.match(LOCKED_BUILDERS);
    if (GO_BUILDER.test(location)) fail(`lock contains forbidden ${location}`);
    if (!match) continue;
    checkVersion(entry.version, `lock ${location}`);
    if (match[1] === 'app-builder-lib' &&
        Object.hasOwn(entry.dependencies || {}, 'app-builder-bin')) {
      fail(`lock ${location} declares forbidden app-builder-bin dependency`);
    }
    if (match[1] === 'electron-builder' || match[1] === 'app-builder-lib') {
      const installed = read(`${location}/package.json`);
      checkVersion(installed.version, `installed ${location}`);
      if (installed.version !== entry.version) {
        fail(`installed ${location} version ${installed.version} must match lock ${entry.version}`);
      }
    }
  }
  const acceptedLibraries = new Set();
  const installedBuilders = [];
  function directory(relative) {
    try {
      const stat = fs.lstatSync(path.join(root, relative));
      return stat.isDirectory() && !stat.isSymbolicLink();
    } catch (error) {
      if (error.code !== 'ENOENT') fail(`installed ${relative} cannot be scanned: ${error.message}`);
      return false;
    }
  }
  // Only descend package / scope directories and their node_modules chains.
  // lstat also excludes junctions, so a link back to an ancestor cannot cycle.
  function scan(modules, scoped = false) {
    if (!directory(modules)) return;
    for (const entry of fs.readdirSync(path.join(root, modules), { withFileTypes: true })) {
      const location = `${modules}/${entry.name}`;
      if (!entry.isDirectory() || entry.isSymbolicLink() || !directory(location)) continue;
      if (!scoped && entry.name.startsWith('@')) { scan(location, true); continue; }
      if (entry.name === 'app-builder-bin') fail(`installed ${location} must not exist`);
      if (entry.name === 'app-builder-lib' || entry.name === 'electron-builder') {
        const before = errors.length;
        checkVersion(read(`${location}/package.json`).version, `installed ${location}`);
        if (entry.name === 'app-builder-lib' && errors.length === before) acceptedLibraries.add(location);
        if (entry.name === 'electron-builder') installedBuilders.push(location);
      }
      scan(`${location}/node_modules`);
    }
  }
  scan('node_modules');
  function resolveLibrary(builder) {
    for (let dir = path.resolve(root, builder); ; dir = path.dirname(dir)) {
      if (path.basename(dir) !== 'node_modules') {
        const candidate = path.join(dir, 'node_modules', 'app-builder-lib');
        if (fs.existsSync(path.join(candidate, 'package.json'))) {
          return path.relative(root, candidate).split(path.sep).join('/');
        }
      }
      if (path.dirname(dir) === dir) return undefined;
    }
  }
  for (const builder of installedBuilders) {
    const resolved = resolveLibrary(builder);
    if (!resolved) fail(`installed ${builder} cannot resolve app-builder-lib`);
    else if (!acceptedLibraries.has(resolved)) {
      fail(`installed ${builder} resolves app-builder-lib to ${resolved}, which was not accepted`);
    }
  }
  return errors;
}

function writeJSON(root, relative, value) {
  const filename = path.join(root, ...relative.split('/'));
  fs.mkdirSync(path.dirname(filename), { recursive: true });
  fs.writeFileSync(filename, JSON.stringify(value));
}

function updateJSON(root, relative, change) {
  const value = JSON.parse(fs.readFileSync(path.join(root, ...relative.split('/')), 'utf8'));
  change(value);
  writeJSON(root, relative, value);
}

function withFixture(version, run) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'cove-appimage-security-'));
  try {
    const devDependencies = { 'electron-builder': version };
    writeJSON(root, 'package.json', {
      devDependencies, build: { appImage: { executableArgs: [] } },
    });
    const packages = { '': { devDependencies } };
    for (const name of ['electron-builder', 'app-builder-lib', 'builder-util', 'dmg-builder']) {
      packages[`node_modules/${name}`] = { version, dependencies: {} };
      writeJSON(root, `node_modules/${name}/package.json`, { name, version });
    }
    writeJSON(root, 'package-lock.json', { lockfileVersion: 3, packages });
    run(root);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}

test('real repo toolchain excludes the vulnerable AppRun generator', () => {
  const errors = checkAppImageBuildSecurity(path.resolve(__dirname, '../..'));
  assert.deepStrictEqual(errors, [], errors.join('\n'));
});

for (const version of ['26.15.0', '26.15.7', '27.0.0']) {
  test(`fixture accepts fixed version ${version}`, () => {
    withFixture(version, root => assert.deepStrictEqual(checkAppImageBuildSecurity(root), []));
  });
}

const cases = [
  ['vulnerable 26.8.1 toolchain', '26.8.1', () => {},
    'lock node_modules/app-builder-lib 26.8.1 is below fixed version 26.15.0'],
  ['M1 electron-builder restored to 26.8.1', '26.15.7', root => {
    updateJSON(root, 'package.json', data => { data.devDependencies['electron-builder'] = '26.8.1'; });
    updateJSON(root, 'package-lock.json', data => {
      data.packages[''].devDependencies['electron-builder'] = '26.8.1';
      data.packages['node_modules/electron-builder'].version = '26.8.1';
    });
    writeJSON(root, 'node_modules/electron-builder/package.json', { version: '26.8.1' });
  }, 'package.json electron-builder 26.8.1 is below fixed version 26.15.0'],
  ['lock/package spec mismatch', '26.15.7', root => {
    updateJSON(root, 'package-lock.json', data => {
      data.packages[''].devDependencies['electron-builder'] = '26.15.0';
    });
  }, 'lock root electron-builder spec must match package.json'],
  ['M3 Go builder reintroduced in lock', '26.15.7', root => {
    updateJSON(root, 'package-lock.json', data => {
      data.packages['node_modules/app-builder-bin'] = { version: '5.0.0' };
    });
  }, 'lock contains forbidden node_modules/app-builder-bin'],
  ['Go builder declared without a lock entry', '26.15.7', root => {
    updateJSON(root, 'package-lock.json', data => {
      data.packages['node_modules/app-builder-lib'].dependencies['app-builder-bin'] = '5.0.0';
    });
  }, 'lock node_modules/app-builder-lib declares forbidden app-builder-bin dependency'],
  ['M4 app-builder-lib just below fix despite toolset configuration', '26.15.7', root => {
    updateJSON(root, 'package.json', data => { data.build.toolsets = { appimage: 'typescript' }; });
    updateJSON(root, 'package-lock.json', data => {
      data.packages['node_modules/app-builder-lib'].version = '26.14.0';
    });
    writeJSON(root, 'node_modules/app-builder-lib/package.json', { version: '26.14.0' });
  }, 'lock node_modules/app-builder-lib 26.14.0 is below fixed version 26.15.0'],
  ['M5 installed version older than lock', '26.15.7', root => {
    writeJSON(root, 'node_modules/app-builder-lib/package.json', { version: '26.15.0' });
  }, 'installed node_modules/app-builder-lib version 26.15.0 must match lock 26.15.7'],
  ['stale installed electron-builder', '26.15.7', root => {
    writeJSON(root, 'node_modules/electron-builder/package.json', { version: '26.8.1' });
  }, 'installed node_modules/electron-builder 26.8.1 is below fixed version 26.15.0'],
  ['stale lock with fixed installed library', '26.15.7', root => {
    updateJSON(root, 'package-lock.json', data => {
      data.packages['node_modules/app-builder-lib'].version = '26.8.1';
    });
  }, 'lock node_modules/app-builder-lib 26.8.1 is below fixed version 26.15.0'],
  ['nested vulnerable app-builder-lib', '26.15.7', root => {
    updateJSON(root, 'package-lock.json', data => {
      data.packages['node_modules/example/node_modules/app-builder-lib'] = { version: '26.8.1' };
    });
  }, 'lock node_modules/example/node_modules/app-builder-lib 26.8.1 is below fixed version 26.15.0'],
  ['installed Go builder directory', '26.15.7', root => {
    fs.mkdirSync(path.join(root, 'node_modules', 'app-builder-bin'));
  }, 'installed node_modules/app-builder-bin must not exist'],
];

for (const spec of ['^26.15.7', '^26.0.12', '~26.15.7', 'latest', '26.15.7-beta', 'garbage']) {
  cases.push([`${spec === '^26.15.7' ? 'M2 ' : ''}reject spec ${spec}`, '26.15.7', root => {
    updateJSON(root, 'package.json', data => { data.devDependencies['electron-builder'] = spec; });
    updateJSON(root, 'package-lock.json', data => {
      data.packages[''].devDependencies['electron-builder'] = spec;
    });
  }, 'package.json electron-builder must be an exact x.y.z version']);
}
for (const location of ['lock', 'installed']) {
  cases.push([`reject prerelease ${location} library`, '26.15.7', root => {
    if (location === 'lock') {
      updateJSON(root, 'package-lock.json', data => {
        data.packages['node_modules/app-builder-lib'].version = '26.15.7-beta';
      });
    } else {
      writeJSON(root, 'node_modules/app-builder-lib/package.json', { version: '26.15.7-beta' });
    }
  }, `${location} node_modules/app-builder-lib must be an exact x.y.z version`]);
}

for (const [name, version, mutate, reason] of cases) {
  test(`fixture rejects ${name}`, t => {
    withFixture(version, root => {
      mutate(root);
      const errors = checkAppImageBuildSecurity(root);
      const failure = errors.find(error => error.startsWith(`${ADVISORY}: ${reason}`));
      assert.ok(failure, errors.join('\n') || 'checker accepted unsafe fixture');
      if (/^M[1-5] /.test(name)) t.diagnostic(`KILLED ${name}: ${failure}`);
    });
  });
}

for (const [location, version] of [
  ['node_modules/electron-builder/node_modules/app-builder-lib', '26.8.1'],
  ['node_modules/some-pkg/node_modules/app-builder-bin', '5.0.0'],
  ['node_modules/@scope/pkg/node_modules/app-builder-lib', '26.14.0'],
  ['node_modules/some-pkg/node_modules/electron-builder', '26.8.1'],
]) {
  test(`fixture rejects unlocked installed ${location}`, () => {
    withFixture('26.15.7', root => {
      writeJSON(root, `${location}/package.json`, { version });
      const errors = checkAppImageBuildSecurity(root);
      const reason = location.endsWith('/app-builder-bin') ? 'must not exist' :
        `${version} is below fixed version 26.15.0`;
      assert.ok(errors.includes(`${ADVISORY}: installed ${location} ${reason}`), errors.join('\n'));
      if (location.startsWith('node_modules/electron-builder/')) {
        assert.ok(errors.includes(`${ADVISORY}: installed node_modules/electron-builder resolves ` +
          `app-builder-lib to ${location}, which was not accepted`), errors.join('\n'));
      }
    });
  });
}
for (const name of ['app-builder-lib', 'app-builder-lib-docs']) {
  test(`fixture accepts unlocked nested ${name}`, () => {
    withFixture('26.15.7', root => {
      writeJSON(root, `node_modules/electron-builder/node_modules/${name}/package.json`,
        { version: name === 'app-builder-lib' ? '26.15.7' : '1.0.0' });
      assert.deepStrictEqual(checkAppImageBuildSecurity(root), []);
    });
  });
}
test('installed scan skips a symlink / junction cycle', t => {
  withFixture('26.15.7', root => {
    const link = path.join(root, 'node_modules', 'cycle', 'node_modules');
    fs.mkdirSync(path.dirname(link));
    try {
      fs.symlinkSync(path.join(root, 'node_modules'), link, process.platform === 'win32' ? 'junction' : 'dir');
    } catch (error) {
      if (!['EPERM', 'EACCES', 'ENOSYS', 'ENOTSUP'].includes(error.code)) throw error;
      t.skip(`symlink / junction creation unavailable: ${error.code}`);
      return;
    }
    assert.deepStrictEqual(checkAppImageBuildSecurity(root), []);
  });
});

const argumentCases = [
  ['A1-M1 removed setting', undefined, undefined, 'appImage'],
  ['A1-M2 unconditional --no-sandbox', ['--no-sandbox'], undefined, 'appImage'],
  ['A1-M3 nonempty reviewed arguments', ['--disable-gpu'], undefined, 'appImage'],
  ['A1-M4 null value', null, undefined, 'appImage'],
  ['string value', '[]', undefined, 'appImage'],
  ['object value', {}, undefined, 'appImage'],
  ['setting only under linux', undefined, [], 'appImage'],
  ['A1-M5 linux nonempty leak', [], ['--no-sandbox'], 'linux'],
  ['linux null value', [], null, 'linux'],
  ['linux string value', [], '[]', 'linux'],
];
for (const [name, appImageArgs, linuxArgs, section] of argumentCases) {
  test(`fixture rejects AppImage arguments: ${name}`, t => {
    withFixture('26.15.7', root => {
      updateJSON(root, 'package.json', data => {
        delete data.build.appImage.executableArgs;
        if (appImageArgs !== undefined) data.build.appImage.executableArgs = appImageArgs;
        if (linuxArgs !== undefined) data.build.linux = { executableArgs: linuxArgs };
      });
      const offending = section === 'appImage' ? appImageArgs : linuxArgs;
      const value = offending === undefined ? '<absent>' : JSON.stringify(offending);
      const reason = `${ADVISORY}: package.json build.${section}.executableArgs must be an empty Array; got ${value}`;
      const errors = checkAppImageBuildSecurity(root);
      assert.ok(errors.includes(reason), errors.join('\n') || 'checker accepted unsafe arguments');
      if (name.startsWith('A1-M')) t.diagnostic(`KILLED ${name}: ${reason}`);
    });
  });
}
for (const linuxArgs of [undefined, []]) {
  test(`fixture accepts appImage [] with linux arguments ${JSON.stringify(linuxArgs)}`, () => {
    withFixture('26.15.7', root => {
      if (linuxArgs !== undefined) {
        updateJSON(root, 'package.json', data => { data.build.linux = { executableArgs: linuxArgs }; });
      }
      assert.deepStrictEqual(checkAppImageBuildSecurity(root), []);
    });
  });
}
