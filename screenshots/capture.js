const { chromium } = require('/opt/node22/lib/node_modules/playwright');
const path = require('path');

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
  const browser = await chromium.launch({ args: ['--no-sandbox', '--disable-setuid-sandbox'] });
  const ctx = await browser.newContext({
    viewport: { width: 390, height: 844 },
    deviceScaleFactor: 2,
  });

  const page = await ctx.newPage();

  // Intercept API calls
  await page.route('**/api/auth', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_AUTH) }));
  await page.route('**/api/leaderboard', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_LEADERBOARD) }));
  await page.route('**/*', route => route.continue());

  const filePath = path.resolve('/home/user/finly/webapp/index.html');
  await page.goto(`file://${filePath}`);

  // Wait for the app to render (past loading screen)
  await page.waitForFunction(() => {
    const app = document.getElementById('app');
    return app && !app.innerText.includes('Загрузка...');
  }, { timeout: 10000 });

  await page.waitForTimeout(600);

  // 1. Home screen
  await page.screenshot({ path: '/home/user/finly/screenshots/01_home.png', fullPage: false });
  console.log('✓ Home');

  // 2. Add screen
  await page.evaluate(() => {
    const navItems = document.querySelectorAll('.ni');
    navItems.forEach(el => { if(el.innerText.includes('Запись')) el.click(); });
  });
  await page.waitForTimeout(400);
  await page.screenshot({ path: '/home/user/finly/screenshots/02_add.png', fullPage: false });
  console.log('✓ Add');

  // 3. Analytics screen
  await page.evaluate(() => {
    const navItems = document.querySelectorAll('.ni');
    navItems.forEach(el => { if(el.innerText.includes('Отчёты')) el.click(); });
  });
  await page.waitForTimeout(400);
  await page.screenshot({ path: '/home/user/finly/screenshots/03_analytics.png', fullPage: false });
  console.log('✓ Analytics');

  // 4. Rewards/Progress screen
  await page.evaluate(() => {
    const navItems = document.querySelectorAll('.ni');
    navItems.forEach(el => { if(el.innerText.includes('Прогресс')) el.click(); });
  });
  await page.waitForTimeout(600);
  await page.screenshot({ path: '/home/user/finly/screenshots/04_rewards.png', fullPage: false });
  console.log('✓ Rewards');

  // 5. Leaderboard tab inside Rewards
  await page.evaluate(() => {
    const tabs = document.querySelectorAll('[style*="border-radius: 12px 12px 0px 0px"], [style*="border-radius:12px 12px 0 0"]');
    tabs.forEach(el => { if(el.innerText.includes('Рейтинг')) el.click(); });
  });
  await page.waitForTimeout(700);
  await page.screenshot({ path: '/home/user/finly/screenshots/05_leaderboard.png', fullPage: false });
  console.log('✓ Leaderboard');

  // 6. Profile screen
  await page.evaluate(() => {
    const navItems = document.querySelectorAll('.ni');
    navItems.forEach(el => { if(el.innerText.includes('Профиль')) el.click(); });
  });
  await page.waitForTimeout(400);
  await page.screenshot({ path: '/home/user/finly/screenshots/06_profile.png', fullPage: false });
  console.log('✓ Profile');

  // 7. Budgets (from profile)
  await page.evaluate(() => {
    document.querySelectorAll('.srow').forEach(el => { if(el.innerText.includes('Бюджеты')) el.click(); });
  });
  await page.waitForTimeout(400);
  await page.screenshot({ path: '/home/user/finly/screenshots/07_budgets.png', fullPage: false });
  console.log('✓ Budgets');

  // 8. Back to home, then open tx detail modal
  await page.evaluate(() => {
    const navItems = document.querySelectorAll('.ni');
    navItems.forEach(el => { if(el.innerText.includes('Главная')) el.click(); });
  });
  await page.waitForTimeout(400);
  // Tap the first transaction card
  await page.evaluate(() => {
    const cards = document.querySelectorAll('.txc');
    if(cards[0]) cards[0].click();
  });
  await page.waitForTimeout(400);
  await page.screenshot({ path: '/home/user/finly/screenshots/08_tx_detail.png', fullPage: false });
  console.log('✓ Tx Detail');

  await browser.close();
  console.log('\nAll screenshots saved to /home/user/finly/screenshots/');
}

main().catch(e => { console.error(e); process.exit(1); });
