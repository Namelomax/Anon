import { LICENSOR } from "@/lib/legal";
import type { BankTransferRecipient, BankTransferSelection } from "@/lib/bank-transfer-types";

/** Публичные банковские реквизиты читает только сервер; неполный набор не публикуется. */
export function bankTransferRecipient(env: NodeJS.ProcessEnv = process.env): BankTransferRecipient | null {
  const bankName = env.BANK_TRANSFER_BANK_NAME?.trim() ?? "";
  const bic = env.BANK_TRANSFER_BIC?.trim() ?? "";
  const account = env.BANK_TRANSFER_ACCOUNT?.trim() ?? "";
  const correspondentAccount = env.BANK_TRANSFER_CORR_ACCOUNT?.trim() ?? "";
  const bankInn = env.BANK_TRANSFER_BANK_INN?.trim() ?? "";
  const bankAddress = env.BANK_TRANSFER_BANK_ADDRESS?.trim() ?? "";
  if (!bankName || bankName.length > 200 || /[\u0000-\u001f\u007f]/.test(bankName) ||
      !/^\d{9}$/.test(bic) || !/^\d{20}$/.test(account) || !/^\d{20}$/.test(correspondentAccount) ||
      (bankInn !== "" && !/^\d{10}$/.test(bankInn)) || bankAddress.length > 500 ||
      /[\u0000-\u001f\u007f]/.test(bankAddress)) {
    return null;
  }
  return {
    name: LICENSOR.fullName, inn: LICENSOR.inn, ogrnip: LICENSOR.ogrnip, address: LICENSOR.address,
    bankName, bic, account, correspondentAccount,
    ...(bankInn ? { bankInn } : {}), ...(bankAddress ? { bankAddress } : {}),
  };
}

export function bankTransferSelection(
  plan: { code: string; title: string; priceKopecks: number; priceYearKopecks: number | null },
  months: 1 | 12,
): BankTransferSelection | null {
  if (plan.code === "trial" || plan.code === "unlimited") return null;
  const amountKopecks = months === 12 ? plan.priceYearKopecks : plan.priceKopecks;
  if (amountKopecks == null || !Number.isSafeInteger(amountKopecks) || amountKopecks <= 0) return null;
  return { plan: plan.code, title: plan.title, months, amountKopecks };
}

/** Назначение идентифицирует аккаунт, но не создаёт счёт или подтверждение платежа. */
export function bankTransferPurpose(accountId: number, planId: number, months: 1 | 12): string {
  return `Оплата лицензии AnonAI. Аккаунт ${accountId}; тариф ${planId}; срок ${months} мес.`;
}
