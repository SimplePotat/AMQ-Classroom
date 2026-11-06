import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright')
const scriptPath = fileURLToPath(new URL('../quiz/static/quiz/main.js', import.meta.url))
const template = (await readFile(new URL('../quiz/templates/quiz/results_template.html', import.meta.url), 'utf8'))
  .replace(/{%[\s\S]*?%}/g, '').replace('{{ i }}', '1')
const browser = await chromium.launch({ headless: true })
try {
  const page = await browser.newPage()
  await page.route('http://legacy.test/**', route => route.fulfill({
    contentType: 'text/html', body: `<!doctype html><div id="results-div"></div>${template}`
  }))
  const open = async () => {
    await page.goto('http://legacy.test/')
    await page.addScriptTag({ path: scriptPath })
  }
  await open()
  const attack = '\"><img src=x onerror="window.__legacyXss=1">'
  await page.evaluate(payload => {
    localStorage.setItem('results', payload)
    initResultsSaving()
  }, attack)
  assert.equal(await page.locator('#results-div img').count(), 0)
  assert.equal(await page.evaluate(() => localStorage.getItem('results')), null)

  await page.locator('.player-field').fill(attack)
  await page.evaluate(() => addPlayer())
  await page.locator('.player-field').last().fill('Second player')
  await page.evaluate(() => addPoint(document.querySelector('[onclick="addPoint(this)"]')))
  await page.waitForFunction(() => JSON.parse(localStorage.getItem('results-v2'))[0].points === 1)
  assert.equal(await page.locator('#results-div img').count(), 0)
  assert.equal(await page.evaluate(() => window.__legacyXss), undefined)

  await open()
  await page.evaluate(() => initResultsSaving())
  assert.equal(await page.locator('.player-field').first().inputValue(), attack)
  assert.equal(await page.locator('.player-field').last().inputValue(), 'Second player')
  assert.equal(await page.locator('.points').first().textContent(), '1')
  assert.equal(await page.locator('#results-div img').count(), 0)
  assert.equal(await page.evaluate(() => window.__legacyXss), undefined)

  await page.evaluate(() => localStorage.setItem('results-v2', '<svg onload="window.__legacyXss=1">'))
  await open()
  await page.evaluate(() => initResultsSaving())
  assert.equal(await page.locator('.player-field').count(), 1)
  assert.equal(await page.evaluate(() => window.__legacyXss), undefined)
  console.log('Legacy browser security checks passed: inert stored HTML, escaped names, safe JSON persistence, new players, scores, malformed storage.')
} finally {
  await browser.close()
}
