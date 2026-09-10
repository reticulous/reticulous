/**
 * onboard_lcd.cpp — first-run setup on a device that has a screen.
 *
 * A fresh node needs the same answers whichever way you reach it: an admin
 * password, a name, a network, and — on a board with a radio — a frequency and
 * a modem configuration. flashmon asks for the first three over
 * serial before the device is ever unplugged; this asks for all of them on the
 * device itself, so a node handed to someone with no cable and no browser is
 * set up from its own panel.
 *
 * It is one modal layer on lv_layer_top — above the status bar and the home-bar
 * strip, opaque and clickable, so nothing behind it can be reached — carrying
 * one step at a time:
 *
 *     password -> hostname -> network -> [network password] ->
 *                 [LoRa frequency -> LoRa modem] -> [mesh name] -> (gone)
 *
 * The bracketed steps are conditional, and that is the shape of the thing: a
 * step appears when the device still has that question open, and never
 * otherwise. Picking an OPEN network joins it on the spot and the password step
 * never happens; a build with no radio never sees the LoRa pair.
 *
 * Every step has Skip, and Skip means "on to the next one", not "abandon the
 * rest" — the answers are independent, and a node that wants a password but no
 * network is an ordinary thing to want. The one exception is the LoRa pair,
 * which is one decision in two windows: skipping the frequency skips the modem
 * window with it, because a modem configuration without a frequency configures
 * nothing.
 *
 * WHAT IT WRITES. Each answer goes through the surface that already owns it —
 * no private state, nothing to reconcile later:
 *   password   authPasswd("admin", "", pw)
 *   hostname   s.net.hostname
 *   network    wifi.cmd.add = "<ssid>\t<pass>"   (net.cpp's task loop)
 *   frequency  lora.0.freq_mhz                    (lora.cpp's unit bridge → Hz)
 *   modem      s.lora.0.{spreading_factor,coding_rate}, lora.0.bw_khz
 *   mesh name  lxmf.cmd.identity_new = "<name>"   (lxmf allocates + keygens)
 * and the radio is enabled (s.lora.0.enable) by the OK on the modem window and
 * nowhere else — so a half-finished LoRa answer leaves the radio off rather
 * than on a frequency nobody confirmed.
 *
 * WHEN IT RUNS. Only on a node that still has one of these open — no admin
 * password, no saved network, no radio frequency, no LXMF identity — and only
 * until it has been answered once: reaching the end (by answering or by
 * skipping) sets
 * s.onboard.done and it never opens again on that device. A factory reset
 * clears the store, and a factory-fresh node asks again, which is the point.
 *
 * Compiled only when spangap-lcd is staged (conditional/spangap-lcd/), and
 * entered from the when:-gated reticulousOnboardInit hook — so a screenless
 * board builds without a line of this in the image. The LoRa pair and the mesh
 * name are gated again, in-source, on iface-lora and lxmf being staged.
 */
#include "lcd.h"
#include "lcd_input.h"
#include "auth.h"
#include "storage.h"
#include "log.h"

#include <esp_random.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

namespace {

/* The steps, in the order they are asked. STEP_DONE is the end marker. */
enum Step {
    STEP_PASSWD = 0,
    STEP_HOSTNAME,
    STEP_WIFI,        /* pick a network from the scan (or "Other network…") */
    STEP_WIFI_PASS,   /* its password — only for a secured or hand-named one */
    STEP_LORA_FREQ,
    STEP_LORA_MODEM,
    STEP_LXMF_NAME,   /* the name this node answers to on the mesh */
    STEP_DONE,
};

/* The whole wizard, since only one can be up and it lives on the lcd task.
 * Every widget pointer is a child of `overlay` and dies with it — nulled on
 * each step rebuild so a late storage callback can't touch a freed object. */
struct {
    lv_obj_t* overlay;      /* the modal layer itself (lv_layer_top child) */
    lv_obj_t* title;
    lv_obj_t* sub;
    lv_obj_t* body;         /* per-step content; cleaned between steps */
    lv_obj_t* msg;          /* the error / hint line under the fields */
    lv_obj_t* btnRow;       /* Back + Skip + primary; a step may hide it and place its own */
    lv_obj_t* backBtn;      /* hidden on the first step this device needs */
    lv_obj_t* okBtn;        /* primary; hidden on steps whose content IS the choice */
    lv_obj_t* okLbl;        /* its label — the text names the step */
    /* Per-step fields. */
    lv_obj_t* pw1;
    lv_obj_t* pw2;
    lv_obj_t* host;
    lv_obj_t* netList;
    lv_obj_t* ssidField;    /* only on the password step, for "Other network…" */
    lv_obj_t* wifiPass;
    lv_obj_t* freqTa;
    lv_obj_t* sfMx;
    lv_obj_t* bwMx;
    lv_obj_t* crMx;
    lv_obj_t* supeCb;
    lv_obj_t* regimeMx;
    lv_obj_t* lxmfName;
    /* Link watch, live only while the network step (and so the scan) stands. */
    lv_timer_t* linkPoll;
    int  step;
    bool needPasswd;
    bool needWifi;
    bool needLora;
    bool needLxmf;
    bool loraSkipped;       /* frequency skipped → the modem window goes with it */
    /* The network selected on the list step, held by NAME so it survives the
     * scan republishing in a different order. Neither set means nothing is
     * selected; `other` means it was not on the list at all and its name is
     * asked for along with its password. */
    char pickedSsid[33];    /* an SSID is at most 32 bytes */
    bool pickedOpen;
    bool pickedOther;
    /* The selection was carried forward by the step's own button. A selection
     * alone must not summon the password step — Skip has to mean skip even
     * after a row was touched. */
    bool wifiPicked;
    bool scanning;
    /* The password this session set, kept so a walk back into that step can
     * change it: authPasswd wants the current one once the realm is no longer
     * unset, and this is the only place that knows it. */
    bool passwdSet;
    char setPasswd[65];
} w;


const lv_color_t COL_FG    = LV_COLOR_MAKE(0xff, 0xff, 0xff);
const lv_color_t COL_MUTED = LV_COLOR_MAKE(0x8a, 0x93, 0xa0);
const lv_color_t COL_OK    = LV_COLOR_MAKE(0x4a, 0xd2, 0x95);
const lv_color_t COL_ERR   = LV_COLOR_MAKE(0xe0, 0x6c, 0x6c);
const lv_color_t COL_BG     = LV_COLOR_MAKE(0x0d, 0x11, 0x17);
const lv_color_t COL_ACCENT = LV_COLOR_MAKE(0x4e, 0x9c, 0xf0);

/* Every field on every step. Bigger than the chrome around it: these are typed
 * into, checked against a piece of paper, and read back across a desk. */
const int FIELD_PX = 16;

/* Hostname charset + cap: a DNS label the device answers to, and what the TLS
 * certificate is issued for. Enforced by the field itself (LVGL rejects the
 * keystroke) rather than validated after the fact. */
const char* HOSTNAME_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_";
const int   HOSTNAME_MAX   = 20;

/* ---- the wizard's own focus group ----
 * Everything focusable here goes in a group of the wizard's own, and the keypad
 * indevs are pointed at it for as long as the modal stands. Two reasons, and
 * the second is the one that bites:
 *   - arrow keys can only reach the wizard's own widgets, not the launcher
 *     tiles sitting behind an opaque layer;
 *   - LVGL moves focus to the next object in the group when the focused one is
 *     deleted, and scrolls it into view. Sharing the shell's group meant that
 *     tearing the wizard down landed focus on some launcher tile and paged the
 *     launcher to wherever it lived — which is why setup used to hand back a
 *     launcher scrolled to its second page.
 * On teardown the indevs go back to the group they had, still focused on
 * whatever the shell left there. */
lv_group_t* s_group = nullptr;
lv_group_t* s_prevGroup = nullptr;

lv_group_t* focusGroup(void) { return s_group ? s_group : lcdInputGroup(); }

bool isKeyIndev(lv_indev_t* in) {
    lv_indev_type_t t = lv_indev_get_type(in);
    return t == LV_INDEV_TYPE_KEYPAD || t == LV_INDEV_TYPE_ENCODER;
}

void grabInput(void) {
    if (s_group) return;
    s_group = lv_group_create();
    for (lv_indev_t* in = lv_indev_get_next(nullptr); in; in = lv_indev_get_next(in)) {
        if (!isKeyIndev(in)) continue;
        if (!s_prevGroup) s_prevGroup = lv_indev_get_group(in);
        lv_indev_set_group(in, s_group);
    }
}

void releaseInput(void) {
    if (!s_group) return;
    lv_group_t* back = s_prevGroup ? s_prevGroup : lcdInputGroup();
    for (lv_indev_t* in = lv_indev_get_next(nullptr); in; in = lv_indev_get_next(in))
        if (isKeyIndev(in)) lv_indev_set_group(in, back);
    lv_group_delete(s_group);          /* after the indevs are off it */
    s_group = nullptr;
    s_prevGroup = nullptr;
}

void showStep(void);
void advance(void);
void nextStep(void);
/* The button handlers, declared here because a step that lays its own buttons
 * out (the frequency keypad) builds them before these are defined. */
void onSkip(lv_event_t* e);
void onOk(lv_event_t* e);
void onBack(lv_event_t* e);
int  priorStep(void);

/* ---- small builders ---- */

lv_obj_t* mkLabel(lv_obj_t* parent, const char* text, LcdFace face, int px, lv_color_t color) {
    lv_obj_t* l = lv_label_create(parent);
    lv_label_set_text(l, text);
    lv_label_set_long_mode(l, LV_LABEL_LONG_WRAP);
    lv_obj_set_width(l, lv_pct(100));
    lv_obj_set_style_text_font(l, lcdFont(face, lcdPx(px)), 0);
    lv_obj_set_style_text_color(l, color, 0);
    return l;
}

lv_obj_t* mkRow(lv_obj_t* parent) {
    lv_obj_t* r = lv_obj_create(parent);
    lv_obj_remove_style_all(r);
    lv_obj_set_width(r, lv_pct(100));
    lv_obj_set_height(r, LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(r, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(r, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(r, lcdPx(6), 0);
    lv_obj_remove_flag(r, LV_OBJ_FLAG_SCROLLABLE);
    return r;
}

lv_obj_t* mkButton(lv_obj_t* parent, const char* text, lv_event_cb_t cb, lv_obj_t** labelOut = nullptr) {
    lv_obj_t* b = lv_button_create(parent);
    lv_obj_set_style_pad_hor(b, lcdPx(10), 0);
    lv_obj_set_style_pad_ver(b, lcdPx(5), 0);
    lv_obj_t* l = lv_label_create(b);
    lv_label_set_text(l, text);
    lv_obj_set_style_text_font(l, lcdFont(LcdFace::UI, lcdPx(13)), 0);
    lv_obj_center(l);
    lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, nullptr);
    if (focusGroup()) lv_group_add_obj(focusGroup(), b);
    if (labelOut) *labelOut = l;
    return b;
}

/* An empty chrome line takes a line of screen with it, and the network list is
 * short of exactly that. Both of these collapse when they have nothing to say. */
void setLine(lv_obj_t* lbl, const char* text) {
    if (!lbl) return;
    lv_label_set_text(lbl, text ? text : "");
    if (text && *text) lv_obj_remove_flag(lbl, LV_OBJ_FLAG_HIDDEN);
    else               lv_obj_add_flag(lbl, LV_OBJ_FLAG_HIDDEN);
}

void setMsg(const char* text, bool ok) {
    if (!w.msg) return;
    lv_obj_set_style_text_color(w.msg, ok ? COL_OK : COL_ERR, 0);
    setLine(w.msg, text);
}

void focusField(lv_obj_t* obj) {
    if (obj && focusGroup()) lv_group_focus_obj(obj);
}

/* ---- typing on a device with no keys ----
 * The panel's own keyboard is the lcd component's (lcdKeyboardAttach in
 * mkField below): a tap on any field puts it up, ✓ writes the text back, and
 * on these one-line fields it fires the field's LV_EVENT_READY as well — so
 * the ✓ is the Enter the step was already written around. The LoRa frequency
 * has its own keypad on the step itself and is never attached. */

/* Where a step OPENS. Focus on a field says "start typing", which is worth
 * having where the keys are under the operator's thumbs and worth nothing where
 * the answer is a tap — and the keyboard is not put up here either: it would
 * cover the question the step is asking and the Skip that answers it another
 * way. On a touch device a step opens as what it is, a question with fields
 * under it. */
void editField(lv_obj_t* obj) {
    if (lcdKeyboardOnScreen()) return;
    focusField(obj);
}

/* The NEXT field of a chain the operator is already typing in (Enter on the
 * first password, on the network name): they are mid-answer, so the keyboard
 * travels with them rather than making them tap again. */
void chainField(lv_obj_t* obj) {
    if (lcdKeyboardOnScreen()) lcdKeyboardOpen(obj);
    else                       focusField(obj);
}

lv_obj_t* mkField(lv_obj_t* parent, const char* placeholder, bool secret, lv_event_cb_t onEnter) {
    lv_obj_t* ta = lv_textarea_create(parent);
    lv_textarea_set_one_line(ta, true);
    lv_textarea_set_password_mode(ta, secret);
    lv_textarea_set_placeholder_text(ta, placeholder);
    lv_obj_set_width(ta, lv_pct(100));
    lv_obj_set_style_pad_all(ta, lcdPx(5), 0);
    lv_obj_set_style_text_font(ta, lcdFont(LcdFace::UI, lcdPx(FIELD_PX)), 0);
    if (focusGroup()) lv_group_add_obj(focusGroup(), ta);
    /* Enter on a one-line textarea is LV_EVENT_READY: it walks to the next
     * field, and on the last one it is the primary button. Typing and pressing
     * return is the whole of every field step. */
    if (onEnter) lv_obj_add_event_cb(ta, onEnter, LV_EVENT_READY, nullptr);
    lcdKeyboardAttach(ta);   /* no keys on the device → a tap types, Enter answers the step */
    return ta;
}

/* ---- a prefilled field that behaves like a selected one ----
 * A field that opens holding the value it already has saves the operator typing
 * it again, and costs them a field to clear when they don't want it. On a
 * desktop the answer is to open with the text selected, so the first keystroke
 * replaces it; LVGL has no programmatic select-all, so this is that behaviour
 * built from what it does have: the value stands, drawn on the accent so it
 * reads as picked-out rather than typed, and the first key that is not
 * navigation clears it and lands in an empty field.
 *
 * Clicking into the field settles it instead of clearing it — a click is how
 * you say "I want to edit this one", exactly as it drops a selection. */
lv_obj_t* s_pristine = nullptr;      /* the field still holding its default, if any */

/* Selected text, drawn the way every other interface draws it: white on the
 * accent BEHIND THE GLYPHS, not a filled field — a filled field is a button.
 * LVGL draws that itself, so this is a real selection rather than something
 * that looks like one: the label carries the range and the LV_PART_SELECTED
 * colours, which is where lv_label's draw reads them from.
 *
 * The caret goes with it. That is half the message — a field with a cursor
 * blinking in it is asking to be added to, while a highlighted run with none is
 * a thing that will be replaced. */
void selectedStyle(lv_obj_t* ta, bool on) {
    lv_obj_t* lbl = lv_textarea_get_label(ta);
    if (lbl) {
        lv_obj_set_style_bg_color(lbl, COL_ACCENT, LV_PART_SELECTED);
        lv_obj_set_style_text_color(lbl, lv_color_white(), LV_PART_SELECTED);
        if (on) {
            /* Byte length, not character count: for ASCII they are the same,
             * and for anything else it overshoots, which selects to the end —
             * which is what "select all" wanted. */
            lv_label_set_text_selection_start(lbl, 0);
            lv_label_set_text_selection_end(lbl, (uint32_t)strlen(lv_textarea_get_text(ta)));
        } else {
            lv_label_set_text_selection_start(lbl, LV_LABEL_TEXT_SELECTION_OFF);
            lv_label_set_text_selection_end(lbl, LV_LABEL_TEXT_SELECTION_OFF);
        }
    }
    lv_obj_set_style_opa(ta, on ? LV_OPA_TRANSP : LV_OPA_COVER, LV_PART_CURSOR);
}

void settlePristine(void) {
    if (!s_pristine) return;
    selectedStyle(s_pristine, false);
    s_pristine = nullptr;
}

void onPristineKey(lv_event_t* e) {
    lv_obj_t* ta = lv_event_get_target_obj(e);
    if (ta != s_pristine) return;
    switch (lv_event_get_key(e)) {
        /* Navigation and the two answers leave the default standing: they are
         * how you accept it, not how you replace it. */
        case LV_KEY_ENTER: case LV_KEY_ESC:
        case LV_KEY_LEFT:  case LV_KEY_RIGHT:
        case LV_KEY_UP:    case LV_KEY_DOWN:
        case LV_KEY_NEXT:  case LV_KEY_PREV:
            return;
        default: break;
    }
    settlePristine();
    lv_textarea_set_text(ta, "");        /* …and the key lands in an empty field */
}

void onPristineClick(lv_event_t*) { settlePristine(); }

/* Fill `ta` with `text` and arm the behaviour above. Empty text arms nothing —
 * there is no default to replace. */
void prefillSelected(lv_obj_t* ta, const char* text) {
    lv_textarea_set_text(ta, text ? text : "");
    if (!text || !*text) return;
    s_pristine = ta;
    selectedStyle(ta, true);
    lv_obj_add_event_cb(ta, onPristineKey,
                        (lv_event_code_t)(LV_EVENT_KEY | LV_EVENT_PREPROCESS), nullptr);
    lv_obj_add_event_cb(ta, onPristineClick, LV_EVENT_CLICKED, nullptr);
}

/* ---- single-choice rows ----
 * A labelled row of mutually exclusive options: a button matrix in one-checked
 * mode, which is radio behaviour in the width a 320 px panel actually has (a
 * column of round radio buttons would put SF alone over two screens). The map
 * must outlive the widget — LVGL keeps the pointer — so every caller passes a
 * static one. */
lv_obj_t* mkChoice(lv_obj_t* parent, const char* label, const char* const map[], int sel) {
    lv_obj_t* r = mkRow(parent);
    lv_obj_t* l = lv_label_create(r);
    lv_label_set_text(l, label);
    lv_obj_set_width(l, lv_pct(22));
    lv_obj_set_style_text_color(l, COL_MUTED, 0);
    lv_obj_set_style_text_font(l, lcdFont(LcdFace::UI, lcdPx(12)), 0);

    lv_obj_t* mx = lv_buttonmatrix_create(r);
    lv_obj_set_flex_grow(mx, 1);
    lv_obj_set_height(mx, lcdPx(26));
    lv_obj_set_style_pad_all(mx, 1, 0);
    lv_obj_set_style_text_font(mx, lcdFont(LcdFace::UI, lcdPx(12)), LV_PART_ITEMS);
    lv_buttonmatrix_set_map(mx, map);
    lv_buttonmatrix_set_button_ctrl_all(mx, LV_BUTTONMATRIX_CTRL_CHECKABLE);
    lv_buttonmatrix_set_one_checked(mx, true);
    if (sel >= 0) lv_buttonmatrix_set_button_ctrl(mx, (uint32_t)sel, LV_BUTTONMATRIX_CTRL_CHECKED);
    if (focusGroup()) lv_group_add_obj(focusGroup(), mx);
    return mx;
}

/* Which option is checked, as an index into the map; -1 if none is. */
int choiceIndex(lv_obj_t* mx, int count) {
    for (int i = 0; i < count; i++)
        if (lv_buttonmatrix_has_button_ctrl(mx, (uint32_t)i, LV_BUTTONMATRIX_CTRL_CHECKED)) return i;
    return -1;
}

/* ---- step 1: the admin password ---- */

/* A generated password is read off this screen and typed somewhere else, once,
 * by someone who cannot ask it what it meant — so every character has to be
 * unmistakable at panel size, and the alphabet pays for that rather than the
 * reader. Out go the pairs that fail on a small screen or in handwriting:
 * l/1/I, O/0, S/5, B/8, Z/2, u/v, and the whole set of letters whose capital is
 * just a bigger copy of the lowercase (c C, k K, o O, p P, s S, v V, w W, x X,
 * z Z) — which is why the case that survives is only where the two shapes
 * genuinely differ. 32 characters over 14 positions is 70 bits, far past
 * anything that will ever be thrown at a device's admin login.
 *
 * `s` is a strong password nobody has to squint at; that is the trade, and it
 * is the right way round. */
const char PW_CHARS[] = "23479abdefghjmnrtyABDEFGHJLMNRTY";
const int  PW_LEN     = 14;

std::string genPassword(int n) {
    std::string s;
    for (int i = 0; i < n; i++) s += PW_CHARS[esp_random() % (sizeof(PW_CHARS) - 1)];
    return s;
}

void onSuggest(lv_event_t*) {
    if (!w.pw1 || !w.pw2) return;
    std::string pw = genPassword(PW_LEN);
    /* Both fields, unmasked, and larger than the field's own type: this screen
     * is the only copy of a generated password, so it has to be readable long
     * enough to be written down — and copied without one character being
     * mistaken for another. Retyping it to confirm what the device itself just
     * generated would prove nothing. */
    lv_textarea_set_text(w.pw1, pw.c_str());
    lv_textarea_set_text(w.pw2, pw.c_str());
    lv_textarea_set_password_mode(w.pw1, false);
    lv_textarea_set_password_mode(w.pw2, false);
    setMsg("Write this down — it is not shown again.", true);
}

void onPwEnter(lv_event_t* e) {
    /* First field: on to the retype. Second: the button. */
    if (lv_event_get_target_obj(e) == w.pw1) chainField(w.pw2);
    else                                     advance();
}

void buildPasswd(void) {
    setLine(w.title, "Set a device password");
    setLine(w.sub, "It protects the web UI and the CLI. Pick something hard to guess.");
    lv_label_set_text(w.okLbl, "Set password");

    w.pw1 = mkField(w.body, "Password", true, onPwEnter);
    w.pw2 = mkField(w.body, "Password again", true, onPwEnter);
    mkButton(w.body, "Suggest one", onSuggest);
    editField(w.pw1);
}

/* Returns true when the step is settled and the wizard may move on. */
bool commitPasswd(void) {
    std::string p1 = w.pw1 ? lv_textarea_get_text(w.pw1) : "";
    std::string p2 = w.pw2 ? lv_textarea_get_text(w.pw2) : "";
    /* Leaving a node with no password is a decision, and it is made by pressing
     * Skip — not by pressing "Set password" with nothing in the field, which is
     * far more likely to be someone who thought they had typed one. */
    if (p1.empty())  { setMsg("Enter a password, or Skip.", false); focusField(w.pw1); return false; }
    if (p1 != p2)    { setMsg("Passwords don't match.", false); focusField(w.pw2); return false; }

    /* Walking Back into this step and setting a different password is a
     * CHANGE, not a first set: the realm stopped being unset the moment the
     * first one was accepted, and authPasswd wants the current one to prove it.
     * This session is the only thing that knows it — it was never stored
     * anywhere in the clear, and it is not going to be. */
    auth_err_t e = authPasswd("admin", w.passwdSet ? w.setPasswd : "", p1.c_str());
    if (e == AUTH_SAME_AS_OTHER_REALM) {
        setMsg("That password is already in use by another realm.", false);
        return false;
    }
    if (e != AUTH_OK) {
        setMsg("Could not set the password.", false);
        warn("onboarding: authPasswd(admin) failed (%d)\n", (int)e);
        return false;
    }
    snprintf(w.setPasswd, sizeof(w.setPasswd), "%s", p1.c_str());
    w.passwdSet = true;
    info("onboarding: admin password set\n");
    return true;
}

/* ---- step 2: the hostname ---- */

void onHostEnter(lv_event_t*) { advance(); }

void buildHostname(void) {
    setLine(w.title, "Name this device");
    setLine(w.sub, "The name it answers to on the network — its web UI is at <name>.local.");
    lv_label_set_text(w.okLbl, "Set Hostname");

    w.host = mkField(w.body, "hostname", false, onHostEnter);
    lv_textarea_set_accepted_chars(w.host, HOSTNAME_CHARS);
    lv_textarea_set_max_length(w.host, HOSTNAME_MAX);
    /* The name it already has, standing as if selected: typing replaces it,
     * Enter accepts it. */
    prefillSelected(w.host, storageGetStr("s.net.hostname", "").c_str());
    mkLabel(w.body, "Letters, digits and _ only.", LcdFace::UI, 11, COL_MUTED);
    editField(w.host);
}

bool commitHostname(void) {
    std::string name = w.host ? lv_textarea_get_text(w.host) : "";
    /* The field can only ever hold legal characters up to its cap, so whatever
     * is in it is usable as-is; empty means "keep the name it has". */
    if (name.empty()) { setMsg("Enter a name, or Skip.", false); focusField(w.host); return false; }
    storageSet("s.net.hostname", name.c_str());
    info("onboarding: hostname %s\n", name.c_str());
    return true;
}

/* ---- step 3: which network ----
 * The whole step is the list: nothing else competes with it for the panel, so
 * it shows what a scan actually found rather than the two or three rows left
 * over beside a form. Picking a row IS the answer — there is no primary button
 * on this step — and what it needs next follows from the row: an open network
 * is joined on the spot, a secured one goes to the password step, and "Other
 * network…" goes there too with its name still to give.
 *
 * The list is the device's own scan (wifi.scan starts it; net republishes
 * wifi.scanned every 20 s while it is set), and joining goes through the
 * wifi.cmd.add sentinel — the same two paths the Settings pane and the browser
 * use. */

void joinWifi(const char* ssid, const char* pass) {
    std::string payload = std::string(ssid) + "\t" + pass;
    storageSet("wifi.cmd.add", payload.c_str());
    info("onboarding: joining \"%s\"\n", ssid);
}

/* Rows SELECT; they do not act. A scroll that ends on a row, a trackball that
 * drifts, a click meant for the row above — none of them can commit anything,
 * because committing is the button at the bottom and always was. The selected
 * row is the only state a mis-click can leave behind, and it is on screen. */
constexpr int PICK_OTHER = -1;      /* the "Other network…" row's index */

void onRowSelect(lv_event_t* e) {
    lv_obj_t* row = lv_event_get_target_obj(e);
    if (!row || !w.netList) return;
    /* One at a time — the checked state IS the selection, so clear the rest. */
    uint32_t n = lv_obj_get_child_count(w.netList);
    for (uint32_t i = 0; i < n; i++)
        lv_obj_remove_state(lv_obj_get_child(w.netList, i), LV_STATE_CHECKED);
    lv_obj_add_state(row, LV_STATE_CHECKED);

    /* Remembered by NAME, not by row: the scan republishes every 20 s sorted by
     * signal, so the index under a selection moves while the operator is
     * looking at it. The name doesn't. */
    int idx = (int)(intptr_t)lv_event_get_user_data(e);
    if (idx == PICK_OTHER) {
        w.pickedSsid[0] = '\0';
        w.pickedOpen  = false;
        w.pickedOther = true;
    } else {
        char key[64];
        snprintf(key, sizeof(key), "wifi.scanned.%d.ssid", idx);
        std::string ssid = storageGetStr(key, "");
        if (ssid.empty()) return;
        snprintf(w.pickedSsid, sizeof(w.pickedSsid), "%s", ssid.c_str());
        snprintf(key, sizeof(key), "wifi.scanned.%d.locked", idx);
        w.pickedOpen  = storageGetStr(key, "") != "1";
        w.pickedOther = false;
    }
    setMsg(nullptr, true);
}

lv_obj_t* addNetRow(const char* text, int idx, bool muted) {
    /* A plain button per row, styled flat — not lv_list. The widget brings its
     * own sizing and theme padding to a container that is already told exactly
     * what to be, and what a list IS here is rows that select: a hairline, a
     * highlight, and nothing raised. */
    lv_obj_t* row = lv_button_create(w.netList);
    lv_obj_set_width(row, lv_pct(100));
    lv_obj_t* lbl = lv_label_create(row);
    lv_label_set_text(lbl, text);
    lv_label_set_long_mode(lbl, LV_LABEL_LONG_DOT);
    lv_obj_set_width(lbl, lv_pct(100));
    lv_obj_set_style_bg_opa(row, LV_OPA_TRANSP, 0);
    lv_obj_set_style_bg_color(row, lv_color_hex(0x2563a0), LV_STATE_CHECKED);
    lv_obj_set_style_bg_opa(row, LV_OPA_COVER, LV_STATE_CHECKED);
    lv_obj_set_style_radius(row, 0, 0);
    lv_obj_set_style_border_width(row, 1, 0);          /* a hairline per row: a list */
    lv_obj_set_style_border_side(row, LV_BORDER_SIDE_BOTTOM, 0);
    lv_obj_set_style_border_color(row, lv_color_hex(0x2a3038), 0);
    lv_obj_set_style_pad_ver(row, lcdPx(6), 0);
    lv_obj_set_style_pad_hor(row, lcdPx(8), 0);
    lv_obj_set_style_text_font(row, lcdFont(LcdFace::UI, lcdPx(FIELD_PX)), 0);
    lv_obj_set_style_text_color(row, muted ? COL_MUTED : COL_FG, 0);
    lv_obj_add_event_cb(row, onRowSelect, LV_EVENT_CLICKED, (void*)(intptr_t)idx);
    if (focusGroup()) lv_group_add_obj(focusGroup(), row);
    return row;
}

void rebuildNets(void) {
    if (!w.netList) return;
    lv_obj_clean(w.netList);
    int n = storageArrayCount("wifi.scanned");
    bool any = false;
    for (int i = 0; i < n; i++) {
        char key[64];
        snprintf(key, sizeof(key), "wifi.scanned.%d.ssid", i);
        std::string ssid = storageGetStr(key, "");
        if (ssid.empty()) continue;
        snprintf(key, sizeof(key), "wifi.scanned.%d.locked", i);
        bool locked = storageGetStr(key, "") == "1";
        /* The exception is what gets marked, and in a word rather than a
         * symbol: nearly every network wants a password, so saying so on every
         * row is noise, and a `*` on the ones that do is a legend the screen
         * has nowhere to put. "(open)" needs no key to read, and it is exactly
         * the difference the next step turns on — the same wording flashmon
         * and the Settings scan list use. */
        lv_obj_t* row = addNetRow((ssid + (locked ? "" : "  (open)")).c_str(), i, false);
        /* The selection follows the name across a re-sort. */
        if (!w.pickedOther && ssid == w.pickedSsid) lv_obj_add_state(row, LV_STATE_CHECKED);
        any = true;
    }
    if (!any) mkLabel(w.netList, "  Looking for networks...", LcdFace::UI, 12, COL_MUTED);
    /* A hidden network is never on the list by definition, so the way to one is
     * always on it. */
    lv_obj_t* other = addNetRow("Other network...", PICK_OTHER, true);
    if (w.pickedOther) lv_obj_add_state(other, LV_STATE_CHECKED);
}

/* net publishes the scan from its own task — hop onto the lcd task to touch
 * LVGL, and let the nulled pointer stand in for "the step has moved on". */
void onScanStorage(const char*, const char*) {
    lcdRun(ON_LCD { rebuildNets(); });
}

bool staConnected(void) { return storageGetStr("wifi.sta.state", "") == "connected"; }

/* The armed scan is not free: net re-scans every 20 s while wifi.scan=1 and
 * does it EVEN WHILE ASSOCIATED (net.cpp's periodic branch runs for
 * ST_STA_CONNECTED too), which on a one-radio part costs the association and
 * gets it back — a 20 s join/rejoin cycle for as long as the step stands.
 *
 * The step only exists because no network was saved, but the screen is not the
 * only way to answer that: the browser, the CLI and flashmon can all put the
 * device on a network while this is up, and then the step is a scan fighting a
 * link nobody wants disturbed. So watch the link while the step stands and, the
 * moment it comes up, take that as the answer and move on. Cheap enough to poll
 * — and polling, not a subscription, because unsubscribing `wifi.sta.state` by
 * scope would take the Settings pane's own binding on that key down with it. */
void onLinkPoll(lv_timer_t*) {
    if (w.step != STEP_WIFI || !staConnected()) return;
    info("onboarding: joined a network from elsewhere — network step answered\n");
    nextStep();          /* stops the scan (and deletes this timer) on the way out */
}

void scanStart(void) {
    if (w.scanning) return;
    w.scanning = true;
    storageSubscribeChanges("wifi.scanned", onScanStorage);
    storageSet("wifi.scan", "1");
    w.linkPoll = lv_timer_create(onLinkPoll, 2000, nullptr);
}

void scanStop(void) {
    if (!w.scanning) return;
    w.scanning = false;
    if (w.linkPoll) { lv_timer_delete(w.linkPoll); w.linkPoll = nullptr; }
    storageUnsubscribeCb("wifi.scanned", onScanStorage);
    storageSet("wifi.scan", "0");
}

void buildWifi(void) {
    setLine(w.title, "Connect to WiFi");
    setLine(w.sub, nullptr);              /* the list gets the line back */
    lv_label_set_text(w.okLbl, "Next");

    w.netList = lv_obj_create(w.body);
    lv_obj_remove_style_all(w.netList);          /* the rows carry the whole look */
    lv_obj_set_width(w.netList, lv_pct(100));
    lv_obj_set_flex_grow(w.netList, 1);
    lv_obj_set_flex_flow(w.netList, LV_FLEX_FLOW_COLUMN);

    rebuildNets();
    scanStart();
}

/* Read the selected row and decide what is still to ask. Nothing here is
 * reached by a stray click: only the button gets this far. */
bool commitWifi(void) {
    if (!w.pickedOther && !w.pickedSsid[0]) {
        setMsg("Pick a network, or Skip.", false);
        return false;
    }
    w.wifiPicked = true;
    if (w.pickedOther) return true;        /* the next step asks for name + password */
    /* An open network has nothing left to ask: join it here, and the password
     * step drops out of the sequence by itself (stepNeeded). */
    if (w.pickedOpen) joinWifi(w.pickedSsid, "");
    return true;
}

/* ---- step 4: that network's password ---- */

void onWifiPassEnter(lv_event_t* e) {
    if (w.ssidField && lv_event_get_target_obj(e) == w.ssidField) chainField(w.wifiPass);
    else                                                          advance();
}

void buildWifiPass(void) {
    setLine(w.title, w.pickedOther ? "Other network" : "Network password");
    if (w.pickedOther) setLine(w.sub, "The name of the network to join, and its password.");
    else               setLine(w.sub, w.pickedSsid);
    lv_label_set_text(w.okLbl, "Connect");

    if (w.pickedOther) {
        w.ssidField = mkField(w.body, "Network name", false, onWifiPassEnter);
        lv_textarea_set_max_length(w.ssidField, 32);
    }
    w.wifiPass = mkField(w.body, "Password (blank if open)", true, onWifiPassEnter);
    editField(w.pickedOther ? w.ssidField : w.wifiPass);
}

bool commitWifiPass(void) {
    std::string ssid = w.pickedOther
                     ? (w.ssidField ? lv_textarea_get_text(w.ssidField) : "")
                     : w.pickedSsid;
    if (ssid.empty()) { setMsg("Give the network's name, or Skip.", false); focusField(w.ssidField); return false; }
    joinWifi(ssid.c_str(), w.wifiPass ? lv_textarea_get_text(w.wifiPass) : "");
    return true;
}

/* ---- steps 5 and 6: the radio ----
 * Only where there is one. A frequency has no default — it is the antenna and
 * the region, which no build can know — so an unset s.lora.0.frequency is
 * exactly "this radio has never been told what it is", and that is what these
 * two windows are for. */
#if CONFIG_STRADDLE_IFACE_LORA

/* The radio's own bounds (iface-lora's LORA_FREQ_MIN_HZ / LORA_FREQ_MAX_HZ,
 * private to that straddle), in the MHz this window speaks. Repeated here to
 * say no to an entry rather than let the unit bridge silently snap it back to
 * whatever was stored. */
const double FREQ_MIN_MHZ = 100.0;
const double FREQ_MAX_MHZ = 2000.0;

/* Phone layout, with the two keys a frequency needs and nothing else. */
const char* const FREQ_KEYS[] = {
    "1", "2", "3", "\n",
    "4", "5", "6", "\n",
    "7", "8", "9", "\n",
    ".", "0", LV_SYMBOL_BACKSPACE, "",
};

void onFreqKey(lv_event_t* e) {
    lv_obj_t* mx = lv_event_get_target_obj(e);
    const char* txt = lv_buttonmatrix_get_button_text(mx, lv_buttonmatrix_get_selected_button(mx));
    if (!txt || !w.freqTa) return;
    if (strcmp(txt, LV_SYMBOL_BACKSPACE) == 0) lv_textarea_delete_char(w.freqTa);
    else                                       lv_textarea_add_text(w.freqTa, txt);
}

void buildLoraFreq(void) {
    setLine(w.title, "LoRa frequency");
    setLine(w.sub, "In MHz.");
    /* This step lays its own buttons out: the keypad wants every row of height
     * it can get, and a full-width button strip under it is the one thing on
     * the screen that can be moved out of its way. Skip and OK go into a
     * column beside it — OK at the bottom, where the thumb already is after the
     * last digit. */
    lv_obj_add_flag(w.btnRow, LV_OBJ_FLAG_HIDDEN);

    /* Bigger than an ordinary field: this is the one number on the device that
     * is read back off the screen and compared against another node's. */
    w.freqTa = lv_textarea_create(w.body);
    lv_textarea_set_one_line(w.freqTa, true);
    lv_textarea_set_accepted_chars(w.freqTa, "0123456789.");
    lv_textarea_set_max_length(w.freqTa, 10);
    lv_textarea_set_placeholder_text(w.freqTa, "868.0");
    lv_obj_set_width(w.freqTa, lv_pct(100));
    lv_obj_set_style_pad_all(w.freqTa, lcdPx(6), 0);
    lv_obj_set_style_text_font(w.freqTa, lcdFont(LcdFace::UI_BOLD, lcdPx(26)), 0);
    lv_obj_set_style_text_align(w.freqTa, LV_TEXT_ALIGN_CENTER, 0);
    lv_textarea_set_text(w.freqTa, storageGetStr("lora.0.freq_mhz", "").c_str());
    if (focusGroup()) lv_group_add_obj(focusGroup(), w.freqTa);
    lv_obj_add_event_cb(w.freqTa, [](lv_event_t*) { advance(); }, LV_EVENT_READY, nullptr);

    /* The keypad is on screen because a board with no keyboard still has to be
     * able to type this, and a board with one loses nothing by it. It and the
     * buttons share one row, and the row takes everything the display above it
     * left — so the keys are as tall as the panel allows. */
    lv_obj_t* row = lv_obj_create(w.body);
    lv_obj_remove_style_all(row);
    lv_obj_set_width(row, lv_pct(100));
    lv_obj_set_flex_grow(row, 1);
    lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
    lv_obj_set_style_pad_column(row, lcdPx(6), 0);
    lv_obj_remove_flag(row, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t* pad = lv_buttonmatrix_create(row);
    lv_obj_set_height(pad, lv_pct(100));
    lv_obj_set_flex_grow(pad, 1);
    lv_obj_set_style_pad_all(pad, 1, 0);
    lv_obj_set_style_text_font(pad, lcdFont(LcdFace::UI, lcdPx(15)), LV_PART_ITEMS);
    lv_buttonmatrix_set_map(pad, FREQ_KEYS);
    lv_obj_add_event_cb(pad, onFreqKey, LV_EVENT_VALUE_CHANGED, nullptr);
    if (focusGroup()) lv_group_add_obj(focusGroup(), pad);

    lv_obj_t* col = lv_obj_create(row);
    lv_obj_remove_style_all(col);
    lv_obj_set_width(col, lcdPx(66));
    lv_obj_set_height(col, lv_pct(100));
    lv_obj_set_flex_flow(col, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(col, LV_FLEX_ALIGN_SPACE_BETWEEN, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_remove_flag(col, LV_OBJ_FLAG_SCROLLABLE);
    /* Same order as the row this step replaces, read top to bottom: the way
     * back, the way past, then the way on — Next at the bottom, where the thumb
     * already is after the last digit. */
    /* Spelt out, never an arrow: this button stands beside a numeric keypad,
     * and a left-arrow next to digits is a backspace to everyone who has ever
     * used one. */
    if (priorStep() >= 0) {
        lv_obj_t* back = mkButton(col, "Back", onBack);
        lv_obj_set_width(back, lv_pct(100));
    }
    lv_obj_t* skip = mkButton(col, "Skip", onSkip);
    lv_obj_set_width(skip, lv_pct(100));
    lv_obj_t* ok = mkButton(col, "Next", onOk);      /* bottom of the column */
    lv_obj_set_width(ok, lv_pct(100));

    focusField(w.freqTa);
}

bool commitLoraFreq(void) {
    std::string txt = w.freqTa ? lv_textarea_get_text(w.freqTa) : "";
    if (txt.empty()) { setMsg("Enter a frequency, or Skip.", false); return false; }
    char* end = nullptr;
    double mhz = strtod(txt.c_str(), &end);
    if (end == txt.c_str() || (end && *end) || mhz < FREQ_MIN_MHZ || mhz > FREQ_MAX_MHZ) {
        setMsg("Not a frequency this radio can be set to.", false);
        return false;
    }
    /* The MHz display key, not the Hz config: it is the entry path lora.cpp
     * offers (its unit bridge converts and applies), so any value works and the
     * conversion lives in one place. */
    storageSet("lora.0.freq_mhz", txt.c_str());
    info("onboarding: LoRa frequency %s MHz\n", txt.c_str());
    return true;
}

/* SF 5..12 (the SX126x range), the three bandwidths a mesh actually shares, and
 * the four coding rates. Static: LVGL keeps the map pointer. */
const char* const SF_KEYS[] = { "5", "6", "7", "8", "9", "10", "11", "12", "" };
const char* const BW_KEYS[] = { "125", "250", "500", "" };
const char* const CR_KEYS[] = { "4/5", "4/6", "4/7", "4/8", "" };
const int SF_FIRST = 5;     /* SF_KEYS[i] is SF (i + SF_FIRST) */
const int CR_FIRST = 5;     /* CR_KEYS[i] is coding rate 4/(i + CR_FIRST) */
/* SUPE's regimes, in the order s.lora.0.SUPE.afa numbers them. The regime says
 * what is permissible on which channels — the raster, the airtime allowance,
 * the length ceilings and the power limit — so it is the second half of the
 * SUPE answer and belongs beside the switch rather than three panes away. */
const char* const REGIME_KEYS[] = { "Single", "EU 9-ch", "" };

/* Where the stored value sits in a map, so the window opens on what the device
 * already holds rather than on a guess. -1 when it holds something the row
 * can't show (a bandwidth typed in the Settings pane, say) — then nothing is
 * checked and OK asks for a pick. */
int indexOfInt(const char* const map[], int count, int value) {
    char want[8];
    snprintf(want, sizeof(want), "%d", value);
    for (int i = 0; i < count; i++) if (strcmp(map[i], want) == 0) return i;
    return -1;
}

void buildLoraModem(void) {
    setLine(w.title, "LoRa Modulation");
    setLine(w.sub, "Every node on the mesh has to agree on these three.");
    lv_label_set_text(w.okLbl, "Enable LoRa");

    int sf = storageGetInt("s.lora.0.spreading_factor", 7);
    int cr = storageGetInt("s.lora.0.coding_rate", 5);
    int bw = atoi(storageGetStr("lora.0.bw_khz", "125").c_str());

    w.sfMx = mkChoice(w.body, "SF", SF_KEYS, (sf >= SF_FIRST && sf <= 12) ? sf - SF_FIRST : -1);
    w.bwMx = mkChoice(w.body, "BW", BW_KEYS, indexOfInt(BW_KEYS, 3, bw));
    w.crMx = mkChoice(w.body, "CR", CR_KEYS, (cr >= CR_FIRST && cr <= 8) ? cr - CR_FIRST : -1);

#if !defined(CONFIG_LORA_NO_SUPE)
    /* Only where the build has it. SUPE is off by default and this is the one
     * decision worth making about it at setup: whether to speak it at all. */
    w.supeCb = lv_checkbox_create(w.body);
    lv_checkbox_set_text(w.supeCb, "Enable SUPE");
    lv_obj_set_style_text_font(w.supeCb, lcdFont(LcdFace::UI, lcdPx(12)), 0);
    lv_obj_set_style_text_color(w.supeCb, COL_FG, 0);
    if (storageGetInt("s.lora.0.SUPE.enable", 0)) lv_obj_add_state(w.supeCb, LV_STATE_CHECKED);
    if (focusGroup()) lv_group_add_obj(focusGroup(), w.supeCb);
    /* Which regime, directly under the switch. It is not a second decision to
     * postpone: a node that speaks SUPE on a different raster from its
     * neighbours negotiates nothing, so the two answers are one answer. */
    w.regimeMx = mkChoice(w.body, "Regime", REGIME_KEYS,
                          storageGetInt("s.lora.0.SUPE.afa", 0) == 1 ? 1 : 0);
#endif
    focusField(w.sfMx);
}

bool commitLoraModem(void) {
    int sf = choiceIndex(w.sfMx, 8);
    int bw = choiceIndex(w.bwMx, 3);
    int cr = choiceIndex(w.crMx, 4);
    if (sf < 0 || bw < 0 || cr < 0) { setMsg("Pick an SF, a BW and a CR, or Skip.", false); return false; }

    /* One batch: the modem settings and the enable commit together, so the
     * radio is never brought up against a half-applied configuration. */
    storageBegin();
    storageSet("s.lora.0.spreading_factor", sf + SF_FIRST);
    storageSet("s.lora.0.coding_rate",      cr + CR_FIRST);
    storageSet("lora.0.bw_khz",             BW_KEYS[bw]);
#if !defined(CONFIG_LORA_NO_SUPE)
    if (w.supeCb)
        storageSet("s.lora.0.SUPE.enable", lv_obj_has_state(w.supeCb, LV_STATE_CHECKED) ? 1 : 0);
    /* Written whatever the switch says: the regime also selects which channels
     * the per-second RSSI beat measures and draws, which is what it does with
     * SUPE off. An unpicked matrix keeps whatever the device already holds. */
    if (w.regimeMx) {
        int rg = choiceIndex(w.regimeMx, 2);
        if (rg >= 0) storageSet("s.lora.0.SUPE.afa", rg);
    }
#endif
    /* The only place the radio is turned on. Reaching this button is the whole
     * of the LoRa answer — a skipped or abandoned pair leaves it off. */
    storageSet("s.lora.0.enable", 1);
    storageEnd();
    info("onboarding: LoRa SF%d BW%s CR4/%d, enabled\n", sf + SF_FIRST, BW_KEYS[bw], cr + CR_FIRST);
    return true;
}

bool loraUnset(void) { return storageGetInt("s.lora.0.frequency", 0) <= 0; }

#else   /* no radio in this build — the pair does not exist */

void buildLoraFreq(void)  {}
void buildLoraModem(void) {}
bool commitLoraFreq(void)  { return true; }
bool commitLoraModem(void) { return true; }
bool loraUnset(void) { return false; }

#endif  /* CONFIG_STRADDLE_IFACE_LORA */

/* ---- last step: the name this node answers to on the mesh ----
 * An LXMF identity is a keypair and a display name, and the name is the only
 * half a person supplies. It goes out with every announce, so it is what other
 * people see when this node turns up — the last thing setup can usefully ask
 * for, and the first thing anyone else will read. */
#if CONFIG_STRADDLE_LXMF

void onLxmfEnter(lv_event_t*) { advance(); }

void buildLxmfName(void) {
    setLine(w.title, "Your name on the mesh");
    setLine(w.sub, "This is the name for your LXMF identity, it's what others on the mesh "
                   "see when they browse around or message you.");
    lv_label_set_text(w.okLbl, "Create identity");

    /* No default, and not the hostname. A hostname is what this box answers to
     * on a network; this is what a PERSON is called on the mesh, and the device
     * has no way to know that. An empty field asks the question honestly, and
     * going on from one is an answer too (no identity). */
    w.lxmfName = mkField(w.body, "Your name", false, onLxmfEnter);
    lv_textarea_set_max_length(w.lxmfName, 32);
    editField(w.lxmfName);
}

bool commitLxmfName(void) {
    std::string name = w.lxmfName ? lv_textarea_get_text(w.lxmfName) : "";
    /* Same rule as the password: going without an identity is a decision, and
     * Skip is how it is made. An empty field under "Create identity" is a
     * mis-press far more often than an answer. */
    if (name.empty()) { setMsg("Give a name, or Skip.", false); focusField(w.lxmfName); return false; }
    /* The sentinel lxmf watches: it allocates the slot, generates the keypair
     * and writes the display name — identity creation belongs to lxmf, and this
     * is the one way in that both the browser and the CLI already use. */
    storageSet("lxmf.cmd.identity_new", name.c_str());
    info("onboarding: LXMF identity \"%s\"\n", name.c_str());
    return true;
}

/* No identity yet — the only state in which asking makes sense. */
bool lxmfUnset(void) { return storageArrayCount("s.lxmf.id") <= 0; }

#else   /* no messaging in this build */

void buildLxmfName(void) {}
bool commitLxmfName(void) { return true; }
bool lxmfUnset(void) { return false; }

#endif  /* CONFIG_STRADDLE_LXMF */

/* ---- the step machine ---- */

/* Nothing here decides whether an answer was given — only whether the step is
 * one this device still has open. Skipping is an answer. */
bool stepNeeded(int step) {
    switch (step) {
        case STEP_PASSWD:   return w.needPasswd;
        case STEP_HOSTNAME: return true;   /* the node's own name; always asked */
        /* Read live, not off the boot-time count: a network can arrive from the
         * browser, the CLI or flashmon while an earlier step stands, and a
         * device that is already on one has nothing left to ask (and must not
         * have a scan armed over its association). */
        case STEP_WIFI:     return w.needWifi && !staConnected();
        /* Only what a carried-forward pick left open: an open network was joined
         * on the way past, and a skipped list carries nothing forward at all. */
        case STEP_WIFI_PASS:
            return w.wifiPicked && (w.pickedOther || (w.pickedSsid[0] && !w.pickedOpen));
        case STEP_LORA_FREQ:
        case STEP_LORA_MODEM: return w.needLora && !w.loraSkipped;
        case STEP_LXMF_NAME:  return w.needLxmf;
        default:            return false;
    }
}

/* The second flush. Everything the wizard handed to another task lands as a
 * PERSISTENT key after the flush below has already run: net writes
 * s.net.wifi.nets.<n> when it takes wifi.cmd.add, lora converts lora.0.freq_mhz
 * into s.lora.0.frequency on its own task, and lxmf writes the identity and its
 * private key some way after identity_new. Storage's own deadline is a minute
 * from the first dirty write, so a device switched off or reset in that minute
 * would come back with the password and the hostname — the two the wizard wrote
 * itself — and none of the rest, which is the worst possible half.
 *
 * Long enough to be past a keypair and a join, short enough that nobody has put
 * the device down yet. */
void onDeferredSave(lv_timer_t*) { storageSave(); }

void finish(void) {
    scanStop();
    storageSet("s.onboard.done", 1);
    lcdKeyboardClose();   /* its own layer — it would outlive the wizard */
    /* Hand the keys back BEFORE the widgets go: LVGL moves focus to the next
     * object in the group as each one is deleted, and the group it does that in
     * must not be the shell's. */
    releaseInput();
    if (w.overlay)   { lv_obj_delete(w.overlay);   w.overlay = nullptr; }
    s_pristine = nullptr;
    w.title = w.sub = w.body = w.msg = w.btnRow = w.backBtn = w.okBtn = w.okLbl = nullptr;
    w.pw1 = w.pw2 = w.host = w.netList = w.ssidField = w.wifiPass = nullptr;
    w.freqTa = w.sfMx = w.bwMx = w.crMx = w.supeCb = w.regimeMx = w.lxmfName = nullptr;
    /* After the screen is handed back, not before: this blocks on the persist
     * worker, and holding the last step up while flash is written would read as
     * the button not having worked. */
    storageSave();
    lv_timer_t* again = lv_timer_create(onDeferredSave, 8000, nullptr);
    lv_timer_set_repeat_count(again, 1);      /* one-shot; deletes itself */
    info("onboarding: finished\n");
}

/* Move to the next step this device still has open, or hand the screen over. */
void nextStep(void) {
    if (w.step == STEP_WIFI) scanStop();
    do { w.step++; } while (w.step < STEP_DONE && !stepNeeded(w.step));
    if (w.step >= STEP_DONE) { finish(); return; }
    showStep();
}

/* The primary button (and the last field's Enter): commit this step, and move
 * on only if it took. A refused commit leaves the step up with its message. */
void advance(void) {
    bool ok = false;
    switch (w.step) {
        case STEP_PASSWD:     ok = commitPasswd();     break;
        case STEP_HOSTNAME:   ok = commitHostname();   break;
        case STEP_WIFI:       ok = commitWifi();       break;
        case STEP_WIFI_PASS:  ok = commitWifiPass();   break;
        case STEP_LORA_FREQ:  ok = commitLoraFreq();   break;
        case STEP_LORA_MODEM: ok = commitLoraModem();  break;
        case STEP_LXMF_NAME:  ok = commitLxmfName();   break;
        default:              ok = true;               break;
    }
    if (ok) nextStep();
}

/* The step before this one that this device needs, or -1 when there is none.
 * Also what decides whether Back is on screen at all: a button that cannot go
 * anywhere should not be there to press. */
int priorStep(void) {
    for (int s = w.step - 1; s >= 0; s--) if (stepNeeded(s)) return s;
    return -1;
}

/* Back changes nothing but which step is up. Whatever an earlier step already
 * committed stays committed — a password is set on the device the moment it is
 * accepted, and walking back to that step is how you change it, not how you
 * undo it. */
void goBack(void) {
    int s = priorStep();
    if (s < 0) return;
    if (w.step == STEP_WIFI) scanStop();
    w.step = s;
    showStep();
}

void onOk(lv_event_t*) { advance(); }
void onBack(lv_event_t*) { goBack(); }

void onSkip(lv_event_t*) {
    /* The two LoRa windows are one decision: a modem configuration on a radio
     * that was never given a frequency configures nothing, so skipping the
     * first takes the second with it. */
    if (w.step == STEP_LORA_FREQ) w.loraSkipped = true;
    /* Skipping the list drops whatever was selected on it, including a
     * selection carried forward before a walk back. */
    if (w.step == STEP_WIFI) w.wifiPicked = false;
    nextStep();
}

void showStep(void) {
    lv_obj_clean(w.body);
    s_pristine = nullptr;      /* whatever held a default is gone with the body */
    w.pw1 = w.pw2 = w.host = w.netList = w.ssidField = w.wifiPass = nullptr;
    w.freqTa = w.sfMx = w.bwMx = w.crMx = w.supeCb = w.regimeMx = w.lxmfName = nullptr;
    setMsg(nullptr, true);
    /* Back to the defaults the steps that say nothing else expect. Back is on
     * screen only where it goes somewhere. */
    lv_obj_remove_flag(w.btnRow, LV_OBJ_FLAG_HIDDEN);
    lv_obj_remove_flag(w.okBtn, LV_OBJ_FLAG_HIDDEN);
    if (priorStep() >= 0) lv_obj_remove_flag(w.backBtn, LV_OBJ_FLAG_HIDDEN);
    else                  lv_obj_add_flag(w.backBtn, LV_OBJ_FLAG_HIDDEN);
    switch (w.step) {
        case STEP_PASSWD:     buildPasswd();     break;
        case STEP_HOSTNAME:   buildHostname();   break;
        case STEP_WIFI:       buildWifi();       break;
        case STEP_WIFI_PASS:  buildWifiPass();   break;
        case STEP_LORA_FREQ:  buildLoraFreq();   break;
        case STEP_LORA_MODEM: buildLoraModem();  break;
        case STEP_LXMF_NAME:  buildLxmfName();   break;
        default: break;
    }
}

void openWizard(void) {
    if (w.overlay) return;
    grabInput();       /* keys reach the wizard and nothing else, until finish() */

    /* Above everything the shell drew: the status bar and the home-bar strip are
     * lv_layer_top children too, so a later sibling covers them, and CLICKABLE
     * on an opaque layer means no gesture reaches what is behind it. */
    lv_obj_t* ov = lv_obj_create(lv_layer_top());
    lv_obj_remove_style_all(ov);
    lv_obj_set_size(ov, lv_pct(100), lv_pct(100));
    lv_obj_set_style_bg_color(ov, COL_BG, 0);
    lv_obj_set_style_bg_opa(ov, LV_OPA_COVER, 0);
    lv_obj_set_style_pad_all(ov, lcdPx(8), 0);
    lv_obj_set_flex_flow(ov, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(ov, lcdPx(5), 0);
    lv_obj_add_flag(ov, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_remove_flag(ov, LV_OBJ_FLAG_SCROLLABLE);
    w.overlay = ov;

    w.title = mkLabel(ov, "", LcdFace::UI_BOLD, 17, COL_FG);
    w.sub   = mkLabel(ov, "", LcdFace::UI, 11, COL_MUTED);

    w.body = lv_obj_create(ov);
    lv_obj_remove_style_all(w.body);
    lv_obj_set_width(w.body, lv_pct(100));
    lv_obj_set_flex_grow(w.body, 1);
    lv_obj_set_flex_flow(w.body, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(w.body, lcdPx(5), 0);

    w.msg = mkLabel(ov, "", LcdFace::UI, 11, COL_ERR);

    /* Back on the left, the two that move forward on the right — the way out of
     * a step is never next to the way on from it. The spacer between them is
     * what holds that when Back is hidden: a hidden object is out of the layout
     * entirely, and without something growing in its place the pair would slide
     * left on exactly the first step, where the primary button most needs to be
     * where the thumb expects it. */
    w.btnRow = lv_obj_create(ov);
    lv_obj_remove_style_all(w.btnRow);
    lv_obj_set_width(w.btnRow, lv_pct(100));
    lv_obj_set_height(w.btnRow, LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(w.btnRow, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(w.btnRow, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER,
                          LV_FLEX_ALIGN_CENTER);
    w.backBtn = mkButton(w.btnRow, "Back", onBack);
    lv_obj_t* gap = lv_obj_create(w.btnRow);
    lv_obj_remove_style_all(gap);
    lv_obj_set_height(gap, 1);
    lv_obj_set_flex_grow(gap, 1);
    lv_obj_t* fwd = lv_obj_create(w.btnRow);
    lv_obj_remove_style_all(fwd);
    lv_obj_set_size(fwd, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(fwd, LV_FLEX_FLOW_ROW);
    lv_obj_set_style_pad_column(fwd, lcdPx(8), 0);
    mkButton(fwd, "Skip", onSkip);
    w.okBtn = mkButton(fwd, "", onOk, &w.okLbl);

    /* Start on the first step this device actually has open. */
    w.step = STEP_PASSWD;
    if (!stepNeeded(w.step)) { nextStep(); return; }
    showStep();
}

}  // namespace

/* Init hook (straddle.yaml `init:`, when: spangap/spangap-lcd). Runs on the boot
 * task with storage, auth and net up, and decides on device state alone — no
 * markers, no first-boot flag beyond the one it sets when it is done.
 *
 * The screen is held dark from boot until the launcher's icons settle, so on a
 * fresh node the modal normally goes up inside that window and the launcher is
 * never the thing that appears. Plain C++ linkage, to match the generated
 * dispatcher's forward declaration. */
void reticulousOnboardInit(void) {
    /* Said on every boot, before anything is decided, because it describes the
     * BUILD and not this boot: this image asks a fresh node's questions on its
     * own screen. A flasher on the other end of the cable needs exactly that,
     * and needs it before it opens a dialog of its own — two surfaces asking
     * for one password at the same time is a race nobody can call. It lands
     * ahead of `spangap ready`, which is the line a flasher can wait for to
     * know whether it is coming. */
    info("setup: on-device\n");

    storageDefault("s.onboard.done", 0);
    if (storageGetInt("s.onboard.done", 0)) return;

    w.needPasswd = authRealmUnset("admin");
    w.needWifi   = storageArrayCount("s.net.wifi.nets") <= 0;
    w.needLora   = loraUnset();
    w.needLxmf   = lxmfUnset();
    if (!w.needPasswd && !w.needWifi && !w.needLora && !w.needLxmf)
        return;                                  /* nothing open — not a fresh node */

    info("onboarding: fresh device (password %s, network %s, radio %s, identity %s)\n",
         w.needPasswd ? "unset" : "set",
         w.needWifi   ? "none"  : "saved",
         w.needLora   ? "unset" : "set",
         w.needLxmf   ? "none"  : "created");
    lcdRun(ON_LCD { openWizard(); });
}
