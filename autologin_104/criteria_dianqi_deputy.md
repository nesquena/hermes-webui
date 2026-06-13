# 機電(副)主任(台南) - 查詢人才條件

> 適用職缺：機電副主任（駐點台南）
> 此檔提供給 `autologin_104.py` 在 104 VIP 查詢人才頁使用

## 1. 基本篩選條件

| 欄位 | 值 | 操作方式 |
|---|---|---|
| 關鍵字 | 機電 水電 | 直接在 `input[name="keyword"]` 輸入 |
| 希望職類 | 水電工程師、水電及其他工程繪圖人員、機電工程師、工地監工／主任 | 點 `[title="希望職務選單"]` → modal 勾選 → 確定 |
| 希望工作地 | 台南市 | 點 `[title="希望工作地選單"]` → modal 勾選 → 確定 |
| 居住地 | 台南市 | 點 `[title="居住地選單"]` → modal 勾選 → 確定 |
| 最近活動日 | 7天內 | radio `name="lastActionDateType"` 直接點選 |
| 總年資 | 3年以上 | dropdown：`workExpTimeMin=3` + `workExpRangeType=up` |
| 科系 | 電機電子工程相關、冷凍空調相關 | 點 `#majorId` → modal 勾選 → 確定 |
| 年齡 | 40 ~ 57 歲 | input `name="agemin"=40`, `name="agemax"=57` |
| 擅長工具 | AutoCAD | 點 `#goodTools` → 選 AutoCAD → 確定 |
| 證照 | 電機類證照（乙級室內配線技術士、丙級室內配線技術士、甲種電匠 等） | 點 `#certificates` → modal 搜尋勾選 → 確定 |

## 2. 隱藏欄位

「年齡」「科系」「擅長工具」「證照」預設隱藏，需先點擊**「更多查詢條件」**展開：

```html
<a data-v-a3bb7260="" href="javascropt:;" gtm-data-listsearch="更多查詢條件">
   更多查詢條件 <i class="vip-icon-arrow-down"></i>
</a>
```

## 3. 送出查詢

填完所有條件後，點擊「符合人數 XXX 人」按鈕：

```html
<button data-v-79407fda=""
        data-gtm-listsearch="一般查詢 - 符合人數按鈕"
        class="btn btn-primary btn--md">
  符合人數 2788XXX 人
</button>
```

CSS Selector：`button[data-gtm-listsearch="一般查詢 - 符合人數按鈕"]`

## 4. modal 操作流程

下列欄位點擊後跳出 `category-picker` modal：
- **希望職類、希望工作地、居住地、科系、擅長工具、證照**

⚠️ 不要點 `<a name="...">` 錨點（無作用），要點旁邊的 `class="form-tag-selector"` 下拉框。

modal 結構：
- 每個選項：`label.category-picker-checkbox > span.children`（顯示文字）+ `input[type="checkbox"]`（值）
- 確定按鈕：`button.category-picker-btn-primary`
- 多層樹狀：可能需點 `.category-item--level-one` 展開分類後才看到目標選項

## 5. 期望結果

按下「符合人數」後跳轉到搜尋結果頁，列出所有符合條件的人選。
