/** Публичные реквизиты получателя и расчёт по выбранному тарифу. Секретов здесь нет. */
export type BankTransferSelection = {
  plan: string;
  title: string;
  months: 1 | 12;
  amountKopecks: number;
};

export type BankTransferRecipient = {
  name: string;
  inn: string;
  ogrnip: string;
  address: string;
  bankInn?: string;
  bankAddress?: string;
  bankName: string;
  bic: string;
  account: string;
  correspondentAccount: string;
};

export type BankTransferQuote = {
  configured: false;
  selection: BankTransferSelection;
  supportEmail: string;
} | {
  configured: true;
  recipient: BankTransferRecipient;
  selection: BankTransferSelection;
  purpose: string;
  supportEmail: string;
};
