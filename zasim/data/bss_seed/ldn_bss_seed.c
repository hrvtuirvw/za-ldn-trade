// SPDX-License-Identifier: GPL-2.0
/*
 * One-shot Nintendo LDN BSS diagnostic for a current mac80211 rtw88 build.
 *
 * The Switch advertises an LDN room with action frames, but this rtw88/mac80211
 * combination does not receive a Probe Response.  While a normal directed scan
 * is already running, this module feeds the Probe Response that ldn 0.0.17's AP
 * implementation would emit into mac80211's public RX API.  mac80211 then owns
 * all parsing and BSS-private initialization; this module never writes private
 * mac80211 structures.
 *
 * This is deliberately a transient hardware diagnostic.  It rejects any
 * non-rtw_8812au device, holds the exact resulting BSS while loaded, and
 * deterministically unlinks that entry on unload.
 */

#include <linux/delay.h>
#include <linux/etherdevice.h>
#include <linux/hex.h>
#include <linux/ieee80211.h>
#include <linux/ktime.h>
#include <linux/module.h>
#include <linux/netdevice.h>
#include <linux/skbuff.h>
#include <net/cfg80211.h>
#include <net/mac80211.h>
#include <net/net_namespace.h>

#define LDN_WLAN_SSID_LEN 32
#define LDN_PROBE_FIXED_LEN offsetof(struct ieee80211_mgmt, u.probe_resp.variable)
#define LDN_FCS_LEN 4

static char *ifname;
module_param(ifname, charp, 0400);
MODULE_PARM_DESC(ifname, "Existing managed rtw88 interface");

static char *bssid;
module_param(bssid, charp, 0400);
MODULE_PARM_DESC(bssid, "Nintendo LDN host BSSID");

static char *ssid;
module_param(ssid, charp, 0400);
MODULE_PARM_DESC(ssid, "32-byte lowercase hexadecimal LDN WLAN SSID");

static int channel = 6;
module_param(channel, int, 0400);
MODULE_PARM_DESC(channel, "LDN social channel");

static struct net_device *held_dev;
static struct wiphy *held_wiphy;
static struct cfg80211_bss *held_bss;
static u8 held_bssid[ETH_ALEN];

static const u8 ldn_rates[] = {
	0x82, 0x84, 0x8b, 0x96, 0x24, 0x30, 0x48, 0x6c,
};

static const u8 ldn_rsn[] = {
	0x01, 0x00,                         /* version */
	0x00, 0x0f, 0xac, 0x04,             /* group: CCMP */
	0x01, 0x00, 0x00, 0x0f, 0xac, 0x04, /* pairwise: CCMP */
	0x01, 0x00, 0x00, 0x0f, 0xac, 0x02, /* AKM: PSK */
	0x0c, 0x00,                         /* capabilities */
};

static bool valid_ldn_ssid(const char *value)
{
	size_t i;

	if (!value || strlen(value) != LDN_WLAN_SSID_LEN)
		return false;
	for (i = 0; i < LDN_WLAN_SSID_LEN; i++)
		if (!((value[i] >= '0' && value[i] <= '9') ||
		      (value[i] >= 'a' && value[i] <= 'f')))
			return false;
	return true;
}

static int append_ie(u8 **position, u8 *end, u8 id,
		     const void *value, u8 length)
{
	if ((size_t)(end - *position) < (size_t)length + 2)
		return -ENOSPC;
	*(*position)++ = id;
	*(*position)++ = length;
	memcpy(*position, value, length);
	*position += length;
	return 0;
}

static int ldn_bss_seed_init(void)
{
	struct wireless_dev *wdev;
	struct ieee80211_channel *chan;
	struct ieee80211_hw *hw;
	struct ieee80211_rx_status *status;
	struct ieee80211_mgmt *mgmt;
	struct sk_buff *skb;
	const char *driver_name;
	const char *stage = "device-lookup";
	u8 frame[LDN_PROBE_FIXED_LEN + 2 + LDN_WLAN_SSID_LEN +
		 2 + sizeof(ldn_rates) + 3 + 2 + sizeof(ldn_rsn) +
		 LDN_FCS_LEN];
	u8 *position;
	u8 *end = frame + sizeof(frame);
	u8 *ie_end = end - LDN_FCS_LEN;
	u8 ds_channel;
	int band;
	int frequency;
	int error;
	int attempt;

	if (!ifname || !bssid || !valid_ldn_ssid(ssid))
		return -EINVAL;
	if (!mac_pton(bssid, held_bssid) ||
	    !is_valid_ether_addr(held_bssid))
		return -EINVAL;
	if (channel >= 1 && channel <= 14)
		band = NL80211_BAND_2GHZ;
	else if (channel == 36 || channel == 40 ||
		 channel == 44 || channel == 48)
		band = NL80211_BAND_5GHZ;
	else
		return -EINVAL;

	held_dev = dev_get_by_name(&init_net, ifname);
	if (!held_dev)
		return -ENODEV;
	stage = "station-validation";
	wdev = held_dev->ieee80211_ptr;
	if (!wdev || !wdev->wiphy || wdev->iftype != NL80211_IFTYPE_STATION) {
		error = -ENODEV;
		goto fail;
	}
	held_wiphy = wdev->wiphy;
	stage = "driver-validation";
	driver_name = dev_driver_string(wiphy_dev(held_wiphy));
	if (!driver_name ||
	    (strcmp(driver_name, "rtw_8812au") &&
	     strcmp(driver_name, "rtw88_8812au"))) {
		error = -EXDEV;
		goto fail;
	}
	if (ether_addr_equal(held_dev->dev_addr, held_bssid)) {
		error = -EINVAL;
		goto fail;
	}

	stage = "channel-validation";
	frequency = ieee80211_channel_to_frequency(channel, band);
	chan = ieee80211_get_channel(held_wiphy, frequency);
	if (!chan || chan->flags & IEEE80211_CHAN_DISABLED) {
		error = -EINVAL;
		goto fail;
	}
	stage = "preexisting-bss-check";
	/* Never merge with or later unlink a BSS that existed before this test. */
	held_bss = cfg80211_get_bss(
		held_wiphy, chan, held_bssid, ssid, LDN_WLAN_SSID_LEN,
		IEEE80211_BSS_TYPE_ESS, IEEE80211_PRIVACY_ON);
	if (held_bss) {
		cfg80211_put_bss(held_wiphy, held_bss);
		held_bss = NULL;
		error = -EEXIST;
		goto fail;
	}

	stage = "frame-build";
	memset(frame, 0, sizeof(frame));
	mgmt = (struct ieee80211_mgmt *)frame;
	mgmt->frame_control = cpu_to_le16(IEEE80211_FTYPE_MGMT |
					    IEEE80211_STYPE_PROBE_RESP);
	ether_addr_copy(mgmt->da, held_dev->dev_addr);
	ether_addr_copy(mgmt->sa, held_bssid);
	ether_addr_copy(mgmt->bssid, held_bssid);
	mgmt->u.probe_resp.timestamp = 0;
	mgmt->u.probe_resp.beacon_int = cpu_to_le16(100);
	/* Match the native Switch beacon observed in the reference PCAP.  The
 	 * upstream AP helper uses 0x0511, but native LDN advertises 0x0411. */
	mgmt->u.probe_resp.capab_info = cpu_to_le16(0x0411);

	position = mgmt->u.probe_resp.variable;
	error = append_ie(&position, ie_end, WLAN_EID_SSID, ssid,
			  LDN_WLAN_SSID_LEN);
	if (error)
		goto fail;
	error = append_ie(&position, ie_end, WLAN_EID_SUPP_RATES,
			  ldn_rates, sizeof(ldn_rates));
	if (error)
		goto fail;
	ds_channel = channel;
	error = append_ie(&position, ie_end, WLAN_EID_DS_PARAMS, &ds_channel, 1);
	if (error)
		goto fail;
	error = append_ie(&position, ie_end, WLAN_EID_RSN, ldn_rsn,
			  sizeof(ldn_rsn));
	if (error)
		goto fail;
	if (position != ie_end) {
		error = -EOVERFLOW;
		goto fail;
	}
	/*
	 * rtw88 advertises IEEE80211_HW_RX_INCLUDES_FCS, so mac80211 always
	 * removes four trailing bytes before parsing this frame.  Supply the
	 * expected FCS storage (zero is sufficient for this internal RX path)
	 * instead of letting it truncate the final RSN IE.
	 */
	memset(ie_end, 0, LDN_FCS_LEN);

	stage = "skb-allocation";
	skb = alloc_skb(sizeof(frame) + 64, GFP_KERNEL);
	if (!skb) {
		error = -ENOMEM;
		goto fail;
	}
	skb_reserve(skb, 64);
	memcpy(skb_put(skb, sizeof(frame)), frame, sizeof(frame));
	skb->dev = held_dev;
	status = IEEE80211_SKB_RXCB(skb);
	memset(status, 0, sizeof(*status));
	status->band = band;
	status->freq = frequency;
	status->signal = -40;
	status->rate_idx = 0;
	status->boottime_ns = ktime_get_boottime_ns();

	/* The active rtw88 USB driver also uses this public irqsafe RX path. */
	stage = "rx-injection";
	hw = wiphy_to_ieee80211_hw(held_wiphy);
	ieee80211_rx_irqsafe(hw, skb);

	/* RX processing is queued.  Hold the exact BSS once mac80211 publishes it. */
	stage = "bss-publication-wait";
	for (attempt = 0; attempt < 25; attempt++) {
		held_bss = cfg80211_get_bss(
			held_wiphy, chan, held_bssid, ssid, LDN_WLAN_SSID_LEN,
			IEEE80211_BSS_TYPE_ESS, IEEE80211_PRIVACY_ON);
		if (held_bss)
			break;
		msleep(20);
	}
	if (!held_bss) {
		error = -EAGAIN;
		goto fail;
	}

	pr_info("ldn_bss_seed: held one temporary LDN BSS on %s channel %d\n",
		ifname, channel);
	return 0;

fail:
	pr_err("ldn_bss_seed: init failed stage=%s error=%d\n", stage, error);
	if (held_bss) {
		cfg80211_unlink_bss(held_wiphy, held_bss);
		cfg80211_put_bss(held_wiphy, held_bss);
		held_bss = NULL;
	}
	held_wiphy = NULL;
	if (held_dev) {
		dev_put(held_dev);
		held_dev = NULL;
	}
	return error;
}

static void ldn_bss_seed_exit(void)
{
	if (held_bss) {
		cfg80211_unlink_bss(held_wiphy, held_bss);
		cfg80211_put_bss(held_wiphy, held_bss);
		held_bss = NULL;
	}
	held_wiphy = NULL;
	if (held_dev) {
		dev_put(held_dev);
		held_dev = NULL;
	}
	pr_info("ldn_bss_seed: removed temporary LDN BSS\n");
}

module_init(ldn_bss_seed_init);
module_exit(ldn_bss_seed_exit);

MODULE_AUTHOR("zatrade diagnostics");
MODULE_DESCRIPTION("Transient Nintendo LDN BSS seed diagnostic for mac80211 rtw88");
MODULE_LICENSE("GPL");
