# CSS + JavaScript + Astro 7-Day Course

Purpose: 7-day beginner-friendly learning track for Kei to become practical with CSS, JavaScript, and Astro, especially for implementing Whimsical Animations-style frontend effects.

Delivery policy:
- Send one short Telegram lesson per day for 7 days.
- Thai explanation, English technical terms/code.
- Beginner-friendly: explain like a smart beginner, no shame/filler.
- Each lesson should be directly usable in Astro.
- Keep Telegram concise enough to read in one sitting; append richer notes here when useful.

Course arc:
1. CSS mental model + selectors + box model + Astro style scope
2. Layout foundations: flex/grid + spacing rhythm
3. Visual CSS: color, typography, border, shadow, responsive units
4. Motion basics: transition, transform, opacity, keyframes
5. JavaScript DOM basics: querySelector, events, classList, state
6. Astro integration: .astro components, scripts, islands, client directives
7. Whimsical-style mini project: animated button/card/SVG/particle-lite + cheatsheet

Learning format per day:
1. วันนี้เรียนอะไร
2. ทำไมสำคัญ
3. Mental model แบบง่าย
4. Syntax ที่ต้องจำ
5. Astro example
6. Exercise 15-30 นาที
7. Common mistakes
8. Mini cheatsheet
9. Tomorrow preview
10. สรุปสั้นๆ

## Day 1 — 2026-05-08

ที่รัก Day 1 เราจะตั้งฐานให้ CSS ไม่ดูเป็นเวทมนตร์นะ

1) วันนี้เอาให้เข้าใจ
CSS คือการเลือก element แล้วใส่ “กฎหน้าตา” ให้มัน: selector บอกว่าเลือกใคร, property/value บอกว่าเปลี่ยนอะไร ใน Astro แต่ละ `.astro` component สามารถมี `<style>` ของตัวเอง และปกติ style จะถูก scope อยู่ใน component นั้น ไม่รั่วไปทั้งเว็บ

2) ภาพจำง่ายๆ
คิดว่า HTML คือกล่องของเล่น, CSS คือสติกเกอร์/ขนาด/ระยะห่างที่แปะบนกล่อง ส่วน box model คือทุก element มี 4 ชั้น: content → padding → border → margin

3) Syntax/Pattern ที่ต้องจำ
`selector { property: value; }` เช่น `.card { padding: 24px; }` ใช้ `.` เลือก class, ใช้ชื่อ tag เลือก element, ใช้ `:hover` เลือกสถานะเวลาเมาส์วาง

4) Astro example
```astro
---
const title = "Tiny magic card";
---

<section class="card">
  <p class="eyebrow">Day 1</p>
  <h2>{title}</h2>
  <button>Hover me</button>
</section>

<style>
  .card {
    box-sizing: border-box;
    max-width: 320px;
    padding: 24px;
    margin: 24px auto;
    border: 2px solid #222;
    border-radius: 20px;
    background: #fff7d6;
  }

  .eyebrow { margin: 0 0 8px; color: #8a5a00; }
  h2 { margin: 0 0 16px; }
  button { padding: 10px 14px; border-radius: 999px; }
  button:hover { background: #222; color: white; }
</style>
```

5) แบบฝึก 20 นาที
สร้าง `MagicCard.astro` แล้ว paste โค้ดนี้ จากนั้นลองเปลี่ยน `padding`, `margin`, `border-radius`, `background` ทีละค่า ดูว่า “กล่อง” เปลี่ยนยังไง แล้วเพิ่ม class ใหม่ชื่อ `.sparkle` ให้ text เล็กๆ ใน card

6) Pitfall วันนี้
อย่าสับสน `padding` กับ `margin`: `padding` คือพื้นที่ “ข้างในกล่อง”, `margin` คือพื้นที่ “นอกกล่อง” และใน Astro ถ้า style ใน component ไม่ไปกระทบไฟล์อื่น นั่นคือ scope ทำงานถูกแล้ว

7) สรุป 1 บรรทัด
CSS เริ่มจากเลือก element ให้ถูก แล้วคุมกล่องด้วย box model ก่อนค่อยทำ animation ให้สวย

## Day 2 — 2026-05-09

ที่รัก Day 2 เราจะทำให้ของบนหน้า “จัดวางเป็น” ก่อนเริ่มขยับสวยๆ

1) วันนี้เอาให้เข้าใจ
Layout คือการตัดสินใจว่า element อยู่ตรงไหน ห่างกันเท่าไหร่ และยืด/หดอย่างไร `flex` เหมาะกับเรียงของเป็นแถวหรือคอลัมน์ เช่นปุ่ม/การ์ดเล็กๆ ส่วน `grid` เหมาะกับพื้นที่ 2 มิติ เช่น gallery หรือ hero ที่มีหลายช่อง Whimsical-style effects จะดูดีขึ้นมากถ้าระยะห่างนิ่งและมี rhythm

2) ภาพจำง่ายๆ
คิดว่า `flex` คือเสียบลูกปัดบนเส้นเดียว ส่วน `grid` คือวางสติกเกอร์บนกระดานช่องๆ ระยะห่าง (`gap`) คือจังหวะหายใจของหน้าเว็บ

3) Syntax/Pattern ที่ต้องจำ
`display: flex; gap: 16px; align-items: center; justify-content: center;` ใช้จัดแนวในหนึ่งแกน
`display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px;` ใช้สร้างหลายคอลัมน์

4) Astro example
```astro
<section class="stage">
  <article class="card">✨ Pop</article>
  <article class="card">🌈 Float</article>
  <article class="card">🫧 Bounce</article>
</section>

<style>
  .stage {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 16px;
    max-width: 560px;
    margin: 32px auto;
    padding: 16px;
  }
  .card {
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 120px;
    border-radius: 24px;
    background: #fff1f8;
    border: 2px solid #222;
  }
</style>
```

5) แบบฝึก 20 นาที
สร้าง `MotionGrid.astro` แล้ว paste โค้ดนี้ ลองเปลี่ยน `gap` เป็น `8px`, `24px`, `40px` แล้วสังเกต mood จากนั้นเปลี่ยน `minmax(140px, 1fr)` เป็น `180px` เพื่อดู responsive behavior

6) Pitfall วันนี้
อย่าใช้ `margin` แยกทุกใบถ้าเป็นรายการหลายชิ้น ใช้ `gap` ที่ parent จะคุมง่ายกว่า และอย่าเริ่ม animation ก่อน layout นิ่ง ไม่งั้นเวลา element ขยับจะรู้สึกมั่ว

7) สรุป 1 บรรทัด
Layout ที่ดีคือเวทีที่นิ่งพอให้ animation ดูมีเวทมนตร์ ไม่ใช่แค่ของขยับไปมา
