// Trimmed from Lab Tycoon 1.1: the purchase path fires these events.
enum Monetization {
    static func bought(_ id: String) {
        Analytics.capture("purchase_initiated", ["product": id])
        Analytics.capture("iap_purchased", ["product": id])
    }
    static func opened() { Analytics.capture("shop_opened") }
}
