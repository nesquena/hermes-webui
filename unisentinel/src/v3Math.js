// v3Math.js
// Helpers for Uniswap V3 price math using sqrtPriceX96

function toBigInt(value) {
  if (typeof value === 'bigint') return value;
  if (value && typeof value.toString === 'function') return BigInt(value.toString());
  return BigInt(value);
}

// Convert sqrtPriceX96 -> price as decimal string with given precision.
// sqrtPriceX96: BigInt or object with toString()
function sqrtPriceX96ToPriceString(sqrtPriceX96, precision = 12) {
  const S = toBigInt(sqrtPriceX96);
  const P = S * S; // BigInt
  const DEN = BigInt(2) ** BigInt(192);
  const integerPart = P / DEN;
  let remainder = P % DEN;

  let frac = '';
  for (let i = 0; i < precision; i++) {
    remainder *= BigInt(10);
    const digit = remainder / DEN;
    frac += digit.toString();
    remainder = remainder % DEN;
  }

  // trim trailing zeros
  frac = frac.replace(/0+$/, '');
  if (frac.length === 0) return integerPart.toString();
  return `${integerPart.toString()}.${frac}`;
}

function sqrtPriceX96ToNumber(sqrtPriceX96, precision = 12) {
  const s = sqrtPriceX96ToPriceString(sqrtPriceX96, precision + 4); // extra digits
  // parseFloat will handle the string to a JS number (may lose precision for very large values)
  return Number(parseFloat(s).toFixed(precision));
}

module.exports = {
  sqrtPriceX96ToPriceString,
  sqrtPriceX96ToNumber,
};
