const { chromium } = require('/opt/node22/lib/node_modules/playwright');
const http = require('http');
const fs = require('fs');
const path = require('path');

// Simple HTTP server to serve the webapp
function startServer() {
  return new Promise((resolve) => {
    const server = http.createServer((req, res) => {
      const filePath = path.join('/home/user/finly/webapp', req.url === '/' ? 'index.html' : req.url);
      try {
        const data = fs.readFileSync(filePath);
        const ext = path.extname(filePath);
        const mime = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css' }[ext] || 'text/plain';
        res.writeHead(200, { 'Content-Type': mime });
        res.end(data);
      } catch {
        res.writeHead(404); res.end('Not found');
      }
    });
    server.listen(0, '127.0.0.1', () => {
      resolve({ server, port: server.address().port });
    });
  });
}

const MOCK_AUTH = {
  user: { streak: 14, xp: 2340, level: 5, streak_record: 21, notifs: true, weekly_report: false, last_log_date: "2026-05-20" },
  txs: [
    { id:1, type:"expense", cat:"🛒", catName:"Продукты", note:"Магнит", amount:45000, dateStr:"2026-05-20", time:"10:15", currency:"UZS", recurring:false, account:"Карта" },
    { id:2, type:"expense", cat:"🍕", catName:"Кафе", note:"Lunch", amount:35000, dateStr:"2026-05-19", time:"13:30", currency:"UZS", recurring:false, account:"Карта" },
    { id:3, type:"income", cat:"💼", catName:"Зарплата", note:"Май", amount:3500000, dateStr:"2026-05-15", time:"09:00", currency:"UZS", recurring:true, account:"Карта" },
    { id:4, type:"expense", cat:"🚌", catName:"Транспорт", note:"Такси", amount:22000, dateStr:"2026-05-14", time:"08:45", currency:"UZS", recurring:false, account:"Карта" },
    { id:5, type:"expense", cat:"🏠", catName:"Жильё", note:"Аренда", amount:800000, dateStr:"2026-05-01", time:"12:00", currency:"UZS", recurring:true, account:"Карта" },
    { id:6, type:"income", cat:"💹", catName:"Подработка", note:"Фриланс", amount:500000, dateStr:"2026-05-10", time:"16:00", currency:"UZS", recurring:false, account:"Карта" },
  ],
  drafts: [],
  budgets: [
    { id:1, name:"Продукты", icon:"🛒", limit:200000, spent:145000, color:"#16A34A" },
    { id:2, name:"Кафе", icon:"🍕", limit:100000, spent:85000, color:"#D97706" },
  ],
  achievements: [
    { id:1, icon:"🔥", name:"7 дней", desc:"Серия 7 дней", earned:true, date:"2026-05-14", xp:50, bg:"#FFF7ED" },
    { id:2, icon:"💰", name:"Первый доход", desc:"Добавить доход", earned:true, date:"2026-05-15", xp:100, bg:"#F0FDF4" },
    { id:3, icon:"📊", name:"Аналитик", desc:"Открыть отчёты", earned:true, date:"2026-05-16", xp:50, bg:"#EFF6FF" },
    { id:4, icon:"🎯", name:"Бюджет", desc:"Создать бюджет", earned:false, locked:false, progress:60, label:"60%", xp:150, bg:"#FEF2F2" },
    { id:5, icon:"🏆", name:"Чемпион", desc:"100 операций", earned:false, locked:true, xp:500, bg:"#FAF5FF" },
    { id:6, icon:"⭐", name:"Золото", desc:"5000 XP", earned:false, locked:false, progress:47, label:"2340/5000", xp:300, bg:"#FFFBEB" },
  ],
  exp_cats: [
    {icon:"🛒",name:"Продукты"},{icon:"🏠",name:"Жильё"},{icon:"🚌",name:"Транспорт"},
    {icon:"🍕",name:"Кафе"},{icon:"💊",name:"Здоровье"},{icon:"🎬",name:"Развлечения"},
    {icon:"👕",name:"Одежда"},{icon:"📚",name:"Образование"},{icon:"🤷",name:"Забыл"},{icon:"•••",name:"Прочее"},
  ],
  inc_cats: [
    {icon:"💼",name:"Зарплата"},{icon:"💹",name:"Подработка"},{icon:"🏦",name:"Инвестиции"},{icon:"🎁",name:"Подарок"},
  ],
  xp_log: [
    {icon:"🔥",label:"Серия сохранена",sub:"10:15 · Streak ×14",pts:20,dateStr:"2026-05-20"},
    {icon:"➕",label:"Добавлена операция",sub:"10:15 · Продукты",pts:10,dateStr:"2026-05-20"},
    {icon:"📊",label:"Открыта аналитика",sub:"09:00",pts:5,dateStr:"2026-05-20"},
  ],
};

const MOCK_LEADERBOARD = [
  { id:1, name:"Абдулазиз", rank:1, xp:2340, level:5, streak:14, is_you:true },
  { id:2, name:"Алишер", rank:2, xp:1980, level:4, streak:7, is_you:false },
  { id:3, name:"Камила", rank:3, xp:1750, level:4, streak:5, is_you:false },
  { id:4, name:"Дилноза", rank:4, xp:1200, level:3, streak:3, is_you:false },
  { id:5, name:"Жавлон", rank:5, xp:980, level:2, streak:1, is_you:false },
];

async function main() {
  const { server, port } = await startServer();
  console.log(`Server running at http://127.0.0.1:${port}`);

  const browser = await chromium.launch({
    headless: true,
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu', '--disable-dev-shm-usage'],
  });
  const ctx = await browser.newContext({
    viewport: { width: 390, height: 844 },
    deviceScaleFactor: 2,
  });

  const page = await ctx.newPage();

  // Log console errors
  page.on('console', msg => { if(msg.type() === 'error') console.log('Console error:', msg.text()); });
  page.on('pageerror', err => console.log('Page error:', err.message));

  // Intercept API calls
  await page.route('**/api/auth', route => {
    console.log('Intercepted /api/auth');
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_AUTH) });
  });
  await page.route('**/api/leaderboard', route => {
    console.log('Intercepted /api/leaderboard');
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_LEADERBOARD) });
  });

  await page.goto(`http://127.0.0.1:${port}/`);
  console.log('Page loaded');

  // Wait for React to render past the loading screen
  await page.waitForFunction(() => {
    const app = document.getElementById('app');
    return app && app.innerText && !app.innerText.includes('Загрузка...');
  }, { timeout: 15000 }).catch(e => console.log('waitForFunction error:', e.message));

  await page.waitForTimeout(1500);

  // Debug: check what's in DOM
  const bodyHtml = await page.evaluate(() => document.getElementById('app')?.innerHTML?.substring(0, 200));
  console.log('App DOM preview:', bodyHtml);

  const dir = '/home/user/finly/screenshots';

  // 1. Home screen
  await page.screenshot({ path: `${dir}/01_home.png` });
  console.log('✓ Home');

  // 2. Add screen - click nav item
  await page.click('.ni:nth-child(2)');
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${dir}/02_add.png` });
  console.log('✓ Add');

  // 3. Analytics screen
  await page.click('.ni:nth-child(3)');
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${dir}/03_analytics.png` });
  console.log('✓ Analytics');

  // 4. Rewards screen
  await page.click('.ni:nth-child(4)');
  await page.waitForTimeout(800);
  await page.screenshot({ path: `${dir}/04_rewards.png` });
  console.log('✓ Rewards');

  // 5. Profile screen
  await page.click('.ni:nth-child(5)');
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${dir}/05_profile.png` });
  console.log('✓ Profile');

  // 6. Home again, tap a tx card
  await page.click('.ni:nth-child(1)');
  await page.waitForTimeout(400);
  const txCard = await page.$('.txc');
  if (txCard) {
    await txCard.click();
    await page.waitForTimeout(400);
    await page.screenshot({ path: `${dir}/06_tx_detail.png` });
    console.log('✓ Tx Detail');
  }

  await browser.close();
  server.close();
  console.log('\nDone! All screenshots in', dir);
}

main().catch(e => { console.error(e); process.exit(1); });
