import { test as base, expect } from '@playwright/test';
import { spawn } from 'node:child_process';
import { cp, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../../../', import.meta.url));

// Starts the disposable surfaces for one site directory and stops them at end of input.
async function serveSurfaces(site, use) {
    const child = spawn('pdm', ['run', 'python', 'tests/browser/test_servers.py', site], {
      cwd: root, stdio: ['pipe', 'pipe', 'pipe'], detached: true,
    });
    let diagnostics = '';
    child.stderr.on('data', data => { diagnostics += data; });
    const lines = createInterface({ input: child.stdout });
    const exited = new Promise(resolve => child.once('close', (...args) => resolve(args)));
    let startup;
    try {
      const ready = new Promise((resolve, reject) => {
        startup = setTimeout(() => reject(new Error('Browser surfaces did not become ready')), 10000);
        child.on('error', reject);
        lines.on('line', line => {
          try {
            const value = JSON.parse(line);
            if (value.docs) resolve(value);
          } catch (error) { reject(error); }
        });
        child.on('exit', code => reject(new Error(`Browser surfaces exited ${code}: ${diagnostics}`)));
      });
      const addresses = await ready;
      clearTimeout(startup);
      await use(addresses);
    } finally {
      clearTimeout(startup);
      child.stdin.end();
      const stop = setTimeout(() => process.kill(-child.pid, 'SIGTERM'), 5000);
      const kill = setTimeout(() => process.kill(-child.pid, 'SIGKILL'), 7000);
      const [code] = await exited;
      clearTimeout(stop);
      clearTimeout(kill);
      lines.close();
      expect(code, diagnostics).toBe(0);
    }
}

const test = base.extend({
  surfaces: [async ({}, use) => serveSurfaces('tmp/docs-site', use), { scope: 'worker', timeout: 20000 }],
  page: async ({ page, surfaces }, use) => {
    const outbound = [];
    await page.context().route('**/*', route => {
      const url = new URL(route.request().url());
      if ([surfaces.docs, surfaces.portal].includes(url.origin)) return route.continue();
      outbound.push(url.origin);
      return route.abort();
    });
    await use(page);
    expect(outbound, 'Browser journeys must use only their owned loopback origins').toEqual([]);
  },
});

test('documentation search survives responsive placement and opens requirement evidence', async ({ page, surfaces }) => {
  await page.goto(surfaces.docs);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await expect(page.locator('.md-sidebar--secondary [data-md-component="search"]')).toBeVisible();
  await page.keyboard.press('ControlOrMeta+k');
  const query = page.getByRole('combobox', { name: 'Search' });
  const result = page.getByRole('link', { name: /^Recognize registry features that are not available/ }).first();
  await expect(query).toBeFocused();
  await query.fill('registry');
  await expect(result).toBeVisible();
  await result.click();
  await expect(page).toHaveURL(/story-registry-capabilities\.html/);
  await page.waitForLoadState('load');
  await expect(page.locator('article')).toContainText(/registry/i);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator('.md-header [data-md-component="search"]')).toHaveCount(1);
  await expect(page.locator('label[for="__search"]')).toBeVisible();
  await page.keyboard.press('ControlOrMeta+k');
  await expect(query).toBeFocused();
  await query.fill('registry');
  await expect(result).toBeVisible();
  await page.keyboard.press('Escape');
  await page.goto(surfaces.docs + '/docs/story-registry-state.html');
  await page.getByRole('link', { name: 'X-17', exact: true }).first().click();
  await expect(page).toHaveURL(/specification\.html#requirement-X-17$/);
  const requirement = page.getByRole('row').filter({ has: page.locator('#requirement-X-17') });
  await expect(requirement).toContainText('following it returns the next items');
  await expect(requirement.getByRole('cell').first()).toBeInViewport();
});

test('an operator filters recorded cases and follows contextual help without changing the verdict', async ({ page, surfaces }) => {
  await page.goto(surfaces.portal + '/runs/' + '1'.repeat(32));
  await expect(page.getByRole('status')).toHaveText('2 of 2 cases');
  await page.getByLabel('Tool', { exact: true }).fill('lookup');
  await page.getByRole('button', { name: 'Filter', exact: true }).click();
  await expect(page.getByRole('status')).toHaveText('1 of 2 cases');
  const cases = page.getByRole('table', { name: 'Acceptance cases', exact: true });
  await expect(cases.getByRole('row').filter({ has: page.getByRole('cell', { name: 'lookup', exact: true }) }).getByRole('cell').nth(4)).toHaveText('failed');
  await expect(page.getByRole('table', { name: 'Tool verdicts' }).getByRole('row').filter({ hasText: 'lookup' }).getByRole('cell').nth(1)).toHaveText('FAIL');
  await expect(cases.getByRole('row')).toHaveCount(2);
  await page.locator('a[href="/help#acceptance"]').click();
  await expect(page).toHaveURL(/\/help#acceptance$/);
  await expect(page.locator('#acceptance')).toBeVisible();
  await expect(page.locator('main')).toContainText('not proof of live-service readiness');
});

test('native run and cancel forms stop controlled work without inventing successful evidence', async ({ page, surfaces }) => {
  await page.goto(surfaces.portal + '/jobs');
  await page.getByLabel('Validation profile').selectOption({ label: 'HTTP benchmark · fixture examples' });
  await page.getByRole('button', { name: 'Start validation run' }).click();
  await expect(page).toHaveURL(/\/jobs\/[a-f0-9]{32}$/);
  await expect(async () => {
    expect(await readFile(surfaces.execution, 'utf8')).toBe('running');
  }).toPass();
  await page.getByRole('link', { name: 'Refresh status' }).click();
  await expect(page.locator('main .badge')).toHaveText('Running');
  await expect(page.locator('main p').filter({ has: page.locator('.badge') })).toContainText('HTTP benchmark · fixture examples');
  await page.getByRole('button', { name: 'Cancel this run' }).click();
  await expect.poll(() => readFile(surfaces.execution, 'utf8')).toBe('stopped');
  await expect(async () => {
    await page.getByRole('link', { name: 'Refresh status' }).click();
    await expect(page.locator('main .badge')).toHaveText('Cancelled');
  }).toPass();
  await expect(page.getByRole('button', { name: 'Cancel this run' })).toHaveCount(0);
  await expect(page.getByRole('link', { name: 'View recorded results' })).toHaveCount(0);
  await expect(page.locator('main')).toContainText('Missing results are not passes');
});

// The unchanged artifact mounted under /nci-sip-mcp/, as GitHub Pages serves a project site.
// static_server.create_server requires an index.html at the served root, so the owned parent has one.
const prefixed = test.extend({
  prefixRoot: [async ({}, use) => {
    const parent = await mkdtemp(join(root, 'tmp', 'pages-prefix-'));
    try {
      await writeFile(join(parent, 'index.html'), '<!doctype html><title>parent</title>\n');
      await cp(join(root, 'tmp', 'docs-site'), join(parent, 'nci-sip-mcp'), { recursive: true });
      await use(parent);
    } finally {
      await rm(parent, { recursive: true, force: true });
    }
  }, { scope: 'worker' }],
  // Depends on prefixRoot, so the server stops before its directory is removed.
  surfaces: [async ({ prefixRoot }, use) => serveSurfaces(prefixRoot, use), { scope: 'worker', timeout: 20000 }],
});

prefixed('the site works under the project-site prefix: search, requirement links and a diagram', async ({ page, surfaces }) => {
  const base = surfaces.docs + '/nci-sip-mcp/';
  await page.goto(base + 'index.html');
  await page.setViewportSize({ width: 1440, height: 1000 });
  await expect(page.locator('.md-sidebar--secondary [data-md-component="search"]')).toBeVisible();
  await page.keyboard.press('ControlOrMeta+k');
  const query = page.getByRole('combobox', { name: 'Search' });
  await expect(query).toBeFocused();
  await query.fill('registry');
  const result = page.getByRole('link', { name: /^Recognize registry features that are not available/ }).first();
  await expect(result).toBeVisible();
  await result.click();
  // The search adds a highlight query (?h=...); the test owns only the origin and the page path.
  await expect(page).toHaveURL(url => url.origin === surfaces.docs
    && url.pathname === '/nci-sip-mcp/docs/story-registry-capabilities.html');
  await page.goto(base + 'docs/story-registry-state.html');
  await page.getByRole('link', { name: 'X-17', exact: true }).first().click();
  await expect(page).toHaveURL(/\/nci-sip-mcp\/.*specification\.html#requirement-X-17$/);
  // The theme replaces each <pre class="mermaid"> source with a <div class="mermaid"> only after
  // mermaid.render succeeds, and puts the SVG in a closed shadow root, so the DOM shows the
  // replacement, not the SVG element itself.
  await page.goto(base + 'docs/caller-permissions.html');
  const diagram = page.locator('article div.mermaid').first();
  await expect(diagram).toBeVisible();
  await expect(page.locator('article pre.mermaid')).toHaveCount(0);
});
