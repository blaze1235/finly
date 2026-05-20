const { chromium } = require('/opt/node22/lib/node_modules/playwright');
const http = require('http');
const fs = require('fs');
const path = require('path');

function startServer(dir) {
  return new Promise((resolve) => {
    const server = http.createServer((req, res) => {
      const url = req.url === '/' ? '/index.html' : req.url;
      const filePath = path.join(dir, url);
      try {
        const data = fs.readFileSync(filePath);
        const ext = path.extname(filePath);
        const mime = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css' }[ext] || 'text/plain';
        res.writeHead(200, { 'Content-Type': mime });
        res.end(data);
      } catch { res.writeHead(404); res.end('Not found'); }
    });
    server.listen(0, '127.0.0.1', () => resolve({ server, port: server.address().port }));
  });
}

const MOCK_AUTH = {
  user: { streak: 14, xp: 2340, level: 5, streak_record: 21, notifs: true, weekly_report: false, last_log_date: "2026-05-20" },
  txs: [
    { id:1, type:"expense", cat:"🛒", catName:"Продукты", note:"Магнит", amount:45000, dateStr:"2026-05-20", time:"10:15", currency:"UZS", recurring:false, account:"Карта" },
    { id:2, type:"expense", cat:"🍕", catName:"Кафе", note:"Ресторан", amount:35000, dateStr:"2026-05-19", time:"13:30", currency:"UZS", recurring:false, account:"Карта" },
    { id:3, type:"income", cat:"💼", catName:"Зарплата", note:"Май 2026", amount:3500000, dateStr:"2026-05-15", time:"09:00", currency:"UZS", recurring:true, account:"Карта" },
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

// Fake Telegram WebApp SDK
const FAKE_TG_SDK = `
window.Telegram = {
  WebApp: {
    ready: function(){},
    expand: function(){},
    initData: "",
    initDataUnsafe: { user: { first_name: "Абдулазиз", username: "alisherjon" } }
  }
};
`;

async function main() {
  const { server, port } = await startServer('/home/user/finly/webapp');
  console.log(`Server at http://127.0.0.1:${port}`);

  const reactJs = fs.readFileSync('/home/user/finly/webapp/react.min.js', 'utf-8');
  const reactDomJs = fs.readFileSync('/home/user/finly/webapp/react-dom.min.js', 'utf-8');

  const browser = await chromium.launch({
    headless: true,
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu', '--disable-dev-shm-usage', '--font-render-hinting=none'],
  });
  const ctx = await browser.newContext({
    viewport: { width: 390, height: 844 },
    deviceScaleFactor: 2,
  });

  const page = await ctx.newPage();

  // Intercept CDN requests for React
  await page.route('**/react-dom@18/**', route =>
    route.fulfill({ status: 200, contentType: 'application/javascript', body: reactDomJs })
  );
  await page.route('**/react@18/**', route =>
    route.fulfill({ status: 200, contentType: 'application/javascript', body: reactJs })
  );
  // Also catch by hostname pattern
  await page.route('*cdn.jsdelivr.net*react*', route => {
    const url = route.request().url();
    if (url.includes('react-dom')) {
      route.fulfill({ status: 200, contentType: 'application/javascript', body: reactDomJs });
    } else {
      route.fulfill({ status: 200, contentType: 'application/javascript', body: reactJs });
    }
  });

  // Mock Telegram SDK
  await page.route('**/telegram-web-app.js', route =>
    route.fulfill({ status: 200, contentType: 'application/javascript', body: FAKE_TG_SDK })
  );

  // Intercept Google Fonts (return empty)
  await page.route('**fonts.googleapis.com/**', route =>
    route.fulfill({ status: 200, contentType: 'text/css', body: '' })
  );
  await page.route('**fonts.gstatic.com/**', route =>
    route.fulfill({ status: 200, contentType: 'font/woff2', body: '' })
  );

  // Intercept API calls
  await page.route('**/api/auth', route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_AUTH) })
  );
  await page.route('**/api/leaderboard', route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_LEADERBOARD) })
  );

  page.on('console', msg => { if(msg.type() === 'error') console.log('ERR:', msg.text()); });
  page.on('pageerror', err => console.log('PageErr:', err.message));

  await page.goto(`http://127.0.0.1:${port}/`);

  // Wait for app to render past loading
  await page.waitForFunction(() => {
    const el = document.getElementById('app');
    return el && el.children.length > 0 && !el.innerText.includes('Загрузка...');
  }, { timeout: 15000 });

  await page.waitForTimeout(1000);

  const appHtml = await page.evaluate(() => document.getElementById('app')?.innerText?.substring(0, 100));
  console.log('App content preview:', appHtml);

  const dir = '/home/user/finly/screenshots';

  // 1. Home
  await page.screenshot({ path: `${dir}/01_home.png` });
  console.log('✓ Home');

  // 2. Add transaction
  await page.evaluate(() => document.querySelectorAll('.ni')[1]?.click());
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${dir}/02_add.png` });
  console.log('✓ Add');

  // 3. Analytics
  await page.evaluate(() => document.querySelectorAll('.ni')[2]?.click());
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${dir}/03_analytics.png` });
  console.log('✓ Analytics');

  // 4. Rewards / Progress
  await page.evaluate(() => document.querySelectorAll('.ni')[3]?.click());
  await page.waitForTimeout(800);
  await page.screenshot({ path: `${dir}/04_rewards_badges.png` });
  console.log('✓ Rewards - Badges');

  // 5. Rewards - Leaderboard tab
  await page.evaluate(() => {
    document.querySelectorAll('div').forEach(el => {
      if (el.innerText?.trim() === 'Рейтинг' && el.style && el.style.cursor === 'pointer') el.click();
    });
  });
  await page.waitForTimeout(800);
  await page.screenshot({ path: `${dir}/05_leaderboard.png` });
  console.log('✓ Leaderboard');

  // 6. Rewards - XP tab
  await page.evaluate(() => {
    document.querySelectorAll('div').forEach(el => {
      if (el.innerText?.trim() === 'Очки' && el.style && el.style.cursor === 'pointer') el.click();
    });
  });
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${dir}/06_xp.png` });
  console.log('✓ XP History');

  // 7. Profile
  await page.evaluate(() => document.querySelectorAll('.ni')[4]?.click());
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${dir}/07_profile.png` });
  console.log('✓ Profile');

  // 8. Budgets (from profile)
  await page.evaluate(() => {
    document.querySelectorAll('.srow').forEach(el => {
      if (el.innerText?.includes('Бюджеты')) el.click();
    });
  });
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${dir}/08_budgets.png` });
  console.log('✓ Budgets');

  // 9. Back to home, open tx detail modal
  await page.evaluate(() => document.querySelectorAll('.ni')[0]?.click());
  await page.waitForTimeout(400);
  const txCard = await page.$('.txc');
  if (txCard) {
    await txCard.click();
    await page.waitForTimeout(400);
    await page.screenshot({ path: `${dir}/09_tx_detail.png` });
    console.log('✓ Transaction Detail');
    // close modal
    await page.keyboard.press('Escape');
    await page.waitForTimeout(300);
  }

  await browser.close();
  server.close();
  console.log('\nDone!');
}

main().catch(e => { console.error(e); process.exit(1); });
