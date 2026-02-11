#include "heatshrink_config.h"

// 32-bit optimized variant. Built if HEATSHRINK_32BIT is set.
#if HEATSHRINK_32BIT


#include <stdlib.h>
#include <string.h>
#include <stdbool.h>
#include <algorithm>
#include "heatshrink_encoder.h"

#include "hs_search.hpp"



extern "C"
{


typedef uint32_t uint_t;
typedef int32_t  int_t;

typedef enum {
    HSES_NOT_FULL,              /* input buffer not full enough */
    HSES_FILLED,                /* buffer is full */
    HSES_SEARCH,                /* searching for patterns */
    HSES_YIELD_TAG_BIT,         /* yield tag bit */
    HSES_YIELD_LITERAL,         /* emit literal byte */
    HSES_YIELD_BR_INDEX,        /* yielding backref index */
    HSES_YIELD_BR_LENGTH,       /* yielding backref length */
    HSES_SAVE_BACKLOG,          /* copying buffer to backlog */
    HSES_FLUSH_BITS,            /* flush bit buffer */
    HSES_DONE,                  /* done */
} HSE_state;

#if HEATSHRINK_DEBUGGING_LOGS
    #if ESP_PLATFORM
        #include "esp_log.h"
        static const char* const TAG = "hsenc";
        #define LOG(...) ESP_LOGD(TAG, __VA_ARGS__)
    #else
        #include <stdio.h>
        #include <ctype.h>
        #define LOG(...) fprintf(stderr, __VA_ARGS__)
    #endif
    
    #include <assert.h>
    #define ASSERT(X) assert(X)
    static const char *state_names[] = {
        "not_full",
        "filled",
        "search",
        "yield_tag_bit",
        "yield_literal",
        "yield_br_index",
        "yield_br_length",
        "save_backlog",
        "flush_bits",
        "done",
    };
#else
    #define LOG(...) /* no-op */
    #define ASSERT(X) /* no-op */
#endif

constexpr uint8_t BIT_INDEX_INIT = 0x0;
            // 0x80;


// Encoder flags
enum {
    FLAG_IS_FINISHING = 0x01,
};

typedef struct {
    uint8_t *buf;               /* output buffer */
    size_t buf_size;            /* buffer size */
    size_t *output_size;        /* bytes pushed to buffer, so far */
} output_info;

#define MATCH_NOT_FOUND ((uint_t)-1)

static uint_t get_input_offset(heatshrink_encoder *hse);
static uint_t get_input_buffer_size(heatshrink_encoder *hse);
static uint_t get_lookahead_size(heatshrink_encoder *hse);
static void add_tag_bit(heatshrink_encoder *hse, output_info *oi, /* u8 */ uint_t tag);
static bool can_take_byte(output_info *oi);
static bool is_finishing(heatshrink_encoder *hse);
static void save_backlog(heatshrink_encoder *hse);

/* Push COUNT (max 8) bits to the output buffer, which has room. */
static void push_bits(heatshrink_encoder *hse, /* u8 */ uint_t count, /* u8 */ uint_t bits,
    output_info *oi);
static /* u8 */ uint_t push_outgoing_bits(heatshrink_encoder *hse, output_info *oi);
static void push_literal_byte(heatshrink_encoder *hse, output_info *oi);

#if HEATSHRINK_DYNAMIC_ALLOC
heatshrink_encoder *heatshrink_encoder_alloc(const uint8_t window_sz2,
        const uint8_t lookahead_sz2) {
    if ((window_sz2 < HEATSHRINK_MIN_WINDOW_BITS) ||
        (window_sz2 > HEATSHRINK_MAX_WINDOW_BITS) ||
        (lookahead_sz2 < HEATSHRINK_MIN_LOOKAHEAD_BITS) ||
        (lookahead_sz2 >= window_sz2)) {
        return NULL;
    }

    /* Note: 2 * the window size is used because the buffer needs to fit
     * (1 << window_sz2) bytes for the current input, and an additional
     * (1 << window_sz2) bytes for the previous buffer of input, which
     * will be scanned for useful backreferences. */
    size_t buf_sz = (2 << window_sz2);

    // TODO buf_sz = (1<<window_sz2) + (1<<lookahead_sz2);

    heatshrink_encoder *hse = (heatshrink_encoder*) HEATSHRINK_MALLOC(sizeof(*hse) + buf_sz);
    if (hse == NULL) { return NULL; }
    hse->window_sz2 = window_sz2;
    hse->lookahead_sz2 = lookahead_sz2;
    heatshrink_encoder_reset(hse);

#if HEATSHRINK_USE_INDEX
    size_t index_sz = buf_sz*sizeof(uint16_t);
    hse->search_index = (hs_index*) HEATSHRINK_MALLOC(index_sz + sizeof(struct hs_index));
    if (hse->search_index == NULL) {
        HEATSHRINK_FREE(hse, sizeof(*hse) + buf_sz);
        return NULL;
    }
    hse->search_index->size = index_sz;
#endif

    LOG("-- allocated encoder with buffer size of %zu (%u byte input size)\n",
        buf_sz, get_input_buffer_size(hse));
    return hse;
}

void heatshrink_encoder_free(heatshrink_encoder *hse) {
#if HEATSHRINK_USE_INDEX
    {
    // const size_t index_sz = sizeof(struct hs_index) + hse->search_index->size;
    HEATSHRINK_FREE(hse->search_index, /*index_sz*/ (sizeof(struct hs_index) + hse->search_index->size));
    }
#endif
    {
    // const size_t buf_sz = (2 << HEATSHRINK_ENCODER_WINDOW_BITS(hse));
    HEATSHRINK_FREE(hse, (sizeof(heatshrink_encoder) + /* buf_sz */ (2 << HEATSHRINK_ENCODER_WINDOW_BITS(hse))));
    }
}
#endif

void heatshrink_encoder_reset(heatshrink_encoder *hse) {
    size_t buf_sz = (2 << HEATSHRINK_ENCODER_WINDOW_BITS(hse));
    memset(hse->buffer, 0, buf_sz);
    hse->input_size = 0;
    hse->state = HSES_NOT_FULL;
    hse->match_scan_index = 0;
    hse->flags = 0;
    hse->bit_index = BIT_INDEX_INIT;
    hse->current_byte = 0x00;
    hse->match_length = 0;

    hse->outgoing_bits = 0x0000;
    hse->outgoing_bits_count = 0;

    #ifdef LOOP_DETECT
    hse->loop_detect = (uint32_t)-1;
    #endif
}

HSE_sink_res heatshrink_encoder_sink(heatshrink_encoder *hse,
        const uint8_t *in_buf, size_t size, size_t *input_size) {
    if ((hse == NULL) || (in_buf == NULL) || (input_size == NULL)) [[unlikely]] {
        return HSER_SINK_ERROR_NULL;
    }

    /* Sinking more content after saying the content is done, tsk tsk */
    if (is_finishing(hse)) [[unlikely]] { return HSER_SINK_ERROR_MISUSE; }

    /* Sinking more content before processing is done */
    if (hse->state != HSES_NOT_FULL) [[unlikely]] { return HSER_SINK_ERROR_MISUSE; }

    uint_t write_offset = get_input_offset(hse) + hse->input_size;
    uint_t ibs = get_input_buffer_size(hse);
    uint_t rem = ibs - hse->input_size;
    uint_t cp_sz = rem < size ? rem : size;

    memcpy(&hse->buffer[write_offset], in_buf, cp_sz);
    *input_size = cp_sz;
    hse->input_size += cp_sz;

    LOG("-- sunk %u bytes (of %zu) into encoder at %d, input buffer now has %u\n",
        cp_sz, size, write_offset, hse->input_size);
    if (cp_sz == rem) {
        LOG("-- internal buffer is now full\n");
        hse->state = HSES_FILLED;
    }

    return HSER_SINK_OK;
}


/***************
 * Compression *
 ***************/

static uint_t find_longest_match(heatshrink_encoder *hse, uint_t start,
    uint_t end, const uint_t maxlen, uint_t& match_length);
static void do_indexing(heatshrink_encoder *hse);

static HSE_state st_step_search(heatshrink_encoder *hse);
static HSE_state st_yield_tag_bit(heatshrink_encoder *hse,
    output_info *oi);
static HSE_state st_yield_literal(heatshrink_encoder *hse,
    output_info *oi);
static HSE_state st_yield_br_index(heatshrink_encoder *hse,
    output_info *oi);
static HSE_state st_yield_br_length(heatshrink_encoder *hse,
    output_info *oi);
static HSE_state st_save_backlog(heatshrink_encoder *hse);
static HSE_state st_flush_bit_buffer(heatshrink_encoder *hse,
    output_info *oi);

HSE_poll_res heatshrink_encoder_poll(heatshrink_encoder *hse,
        uint8_t *out_buf, size_t out_buf_size, size_t *output_size) {
    if ((hse == NULL) || (out_buf == NULL) || (output_size == NULL)) [[unlikely]] {
        return HSER_POLL_ERROR_NULL;
    }
    if (out_buf_size == 0) [[unlikely]] {
        LOG("-- MISUSE: output buffer size is 0\n");
        return HSER_POLL_ERROR_MISUSE;
    }
    *output_size = 0;

    output_info oi;
    oi.buf = out_buf;
    oi.buf_size = out_buf_size;
    oi.output_size = output_size;

    while (1) {
        LOG("-- polling, state %u (%s), flags 0x%02x\n",
            hse->state, state_names[hse->state], hse->flags);

        /* u8 */ uint_t in_state = hse->state;
        switch (in_state) {
        case HSES_NOT_FULL:
            return HSER_POLL_EMPTY;
        case HSES_FILLED:
            do_indexing(hse);
            hse->state = HSES_SEARCH;
            break;
        case HSES_SEARCH:
            hse->state = st_step_search(hse);
            break;
        case HSES_YIELD_TAG_BIT:
            hse->state = st_yield_tag_bit(hse, &oi);
            break;
        case HSES_YIELD_LITERAL:
            hse->state = st_yield_literal(hse, &oi);
            break;
        case HSES_YIELD_BR_INDEX:
            hse->state = st_yield_br_index(hse, &oi);
            break;
        case HSES_YIELD_BR_LENGTH:
            hse->state = st_yield_br_length(hse, &oi);
            break;
        case HSES_SAVE_BACKLOG:
            hse->state = st_save_backlog(hse);
            break;
        case HSES_FLUSH_BITS:
            hse->state = st_flush_bit_buffer(hse, &oi);
            // [[fallthrough]];
            break;
        case HSES_DONE:
            return HSER_POLL_EMPTY;
        default:
            [[unlikely]]
            LOG("-- bad state %s\n", state_names[hse->state]);
            return HSER_POLL_ERROR_MISUSE;
        }

        if (hse->state == in_state) {
            /* Check if output buffer is exhausted. */
            if (*output_size == out_buf_size) return HSER_POLL_MORE;
        }
    }
}

HSE_finish_res heatshrink_encoder_finish(heatshrink_encoder *hse) {
    if (hse == NULL) [[unlikely]] { return HSER_FINISH_ERROR_NULL; }
    LOG("-- setting is_finishing flag\n");
    hse->flags |= FLAG_IS_FINISHING;
    if (hse->state == HSES_NOT_FULL) { hse->state = HSES_FILLED; }
    return hse->state == HSES_DONE ? HSER_FINISH_DONE : HSER_FINISH_MORE;
}

static HSE_state st_step_search(heatshrink_encoder *hse) {
    uint_t start;
    uint_t end;
    uint_t max_possible;
    {
        const uint_t lookahead_sz = get_lookahead_size(hse);
        const uint_t msi = hse->match_scan_index;
        #if HEATSHRINK_DEBUGGING_LOGS
        {
        const uint_t window_length = get_input_buffer_size(hse);
        LOG("## step_search, scan @ +%d (%d/%d), input size %d\n",
            msi, hse->input_size + msi, 2*window_length, hse->input_size);
        }
        #endif
        {
            const bool fin = is_finishing(hse);
            // This rewritten if alone yields ~5% overall speedup for the SIMD version :-o
            if(fin) [[unlikely]] {
                if(msi >= hse->input_size) {
                    return HSES_FLUSH_BITS;
                }
            } else {
                if(msi > (hse->input_size - lookahead_sz)) {
                    return HSES_SAVE_BACKLOG;
                }
            }
            // if (msi > hse->input_size - (fin ? 1 : lookahead_sz)) {
            //     /* Current search buffer is exhausted, copy it into the
            //     * backlog and await more input. */
            //     LOG("-- end of search @ %d\n", msi);
            //     return fin ? HSES_FLUSH_BITS : HSES_SAVE_BACKLOG;
            // }
        }

        end = get_input_offset(hse) + msi;
        start = end - get_input_buffer_size(hse) /* window_length */;
        max_possible = std::min((uint_t)(hse->input_size-msi), lookahead_sz);
    }

    uint_t match_length = 0;
    const uint_t match_pos = find_longest_match(hse,
        start, end, max_possible, match_length);

    if (match_pos == MATCH_NOT_FOUND) {
        LOG("ss Match not found\n");
        hse->match_scan_index++;
        hse->match_length = 0;
        return HSES_YIELD_TAG_BIT;
    } else {
        LOG("ss Found match of %d bytes at %d\n", match_length, match_pos);
        hse->match_pos = match_pos;
        hse->match_length = match_length;
        ASSERT(match_pos <= 1 << HEATSHRINK_ENCODER_WINDOW_BITS(hse) /*window_length*/);

        return HSES_YIELD_TAG_BIT;
    }
}

static HSE_state st_yield_tag_bit(heatshrink_encoder *hse,
        output_info *oi) {
    if (can_take_byte(oi)) {
        if (hse->match_length == 0) {
            add_tag_bit(hse, oi, HEATSHRINK_LITERAL_MARKER);
            return HSES_YIELD_LITERAL;
        } else {
            add_tag_bit(hse, oi, HEATSHRINK_BACKREF_MARKER);
            hse->outgoing_bits = hse->match_pos - 1;
            hse->outgoing_bits_count = HEATSHRINK_ENCODER_WINDOW_BITS(hse);
            return HSES_YIELD_BR_INDEX;
        }
    } else {
        return HSES_YIELD_TAG_BIT; /* output is full, continue */
    }
}

static HSE_state st_yield_literal(heatshrink_encoder *hse,
        output_info *oi) {
    if (can_take_byte(oi)) {
        push_literal_byte(hse, oi);
        return HSES_SEARCH;
    } else {
        return HSES_YIELD_LITERAL;
    }
}

static HSE_state st_yield_br_index(heatshrink_encoder *hse,
        output_info *oi) {
    if (can_take_byte(oi)) {
        LOG("-- yielding backref index %u\n", hse->match_pos);
        if (push_outgoing_bits(hse, oi) > 0) {
            return HSES_YIELD_BR_INDEX; /* continue */
        } else {
            hse->outgoing_bits = hse->match_length - 1;
            hse->outgoing_bits_count = HEATSHRINK_ENCODER_LOOKAHEAD_BITS(hse);
            return HSES_YIELD_BR_LENGTH; /* done */
        }
    } else {
        return HSES_YIELD_BR_INDEX; /* continue */
    }
}

static HSE_state st_yield_br_length(heatshrink_encoder *hse,
        output_info *oi) {
    if (can_take_byte(oi)) {
        LOG("-- yielding backref length %u\n", hse->match_length);
        if (push_outgoing_bits(hse, oi) > 0) {
            return HSES_YIELD_BR_LENGTH;
        } else {
            hse->match_scan_index += hse->match_length;
            hse->match_length = 0;
            return HSES_SEARCH;
        }
    } else {
        return HSES_YIELD_BR_LENGTH;
    }
}

static HSE_state st_save_backlog(heatshrink_encoder *hse) {
    LOG("-- saving backlog\n");
    save_backlog(hse);
    return HSES_NOT_FULL;
}

static HSE_state st_flush_bit_buffer(heatshrink_encoder *hse,
        output_info *oi) {
    if (hse->bit_index == BIT_INDEX_INIT) {
        LOG("-- done!\n");
        return HSES_DONE;
    } else if (can_take_byte(oi)) {
        LOG("-- flushing remaining byte (bit_index == 0x%02x)\n", hse->bit_index);
        oi->buf[(*oi->output_size)++] = (hse->current_byte << (8-hse->bit_index));
        LOG("-- done!\n");
        return HSES_DONE;
    } else {
        return HSES_FLUSH_BITS;
    }
}

static void add_tag_bit(heatshrink_encoder *hse, output_info *oi, /* u8 */ uint_t tag) {
    LOG("-- adding tag bit: %d\n", tag);
    push_bits(hse, 1, tag, oi);
}

static uint_t get_input_offset(heatshrink_encoder *hse) {
    return (1 << HEATSHRINK_ENCODER_WINDOW_BITS(hse));
    (void)hse;
}


static uint_t get_input_buffer_size(heatshrink_encoder *hse) {
    return (1 << HEATSHRINK_ENCODER_WINDOW_BITS(hse));
    (void)hse;
}

static uint_t get_lookahead_size(heatshrink_encoder *hse) {
    return (1 << HEATSHRINK_ENCODER_LOOKAHEAD_BITS(hse));
    (void)hse;
}

static void do_indexing(heatshrink_encoder *hse) {
#if HEATSHRINK_USE_INDEX
    /* Build an index array I that contains flattened linked lists
     * for the previous instances of every byte in the buffer.
     *
     * For example, if buf[200] == 'x', then index[200] will either
     * be an offset i such that buf[i] == 'x', or a negative offset
     * to indicate end-of-list. This significantly speeds up matching,
     * while only using sizeof(uint16_t)*sizeof(buffer) bytes of RAM.
     *
     * Future optimization options:
     * 1. Since any negative value represents end-of-list, the other
     *    15 bits could be used to improve the index dynamically.
     *
     * 2. Likewise, the last lookahead_sz bytes of the index will
     *    not be usable, so temporary data could be stored there to
     *    dynamically improve the index.
     * */
    struct hs_index *hsi = HEATSHRINK_ENCODER_INDEX(hse);
    int16_t last[256];
    memset(last, 0xFF, sizeof(last));

    uint8_t * const data = hse->buffer;
    int16_t * const index = hsi->index;

    const uint_t input_offset = get_input_offset(hse);
    const uint_t end = input_offset + hse->input_size;

    for (uint_t i=0; i<end; i++) {
        /* u8 */ uint_t v = data[i];
        int_t lv = last[v];
        index[i] = lv;
        last[v] = i;
    }
#endif
}

static bool is_finishing(heatshrink_encoder *hse) {
    return (hse->flags & FLAG_IS_FINISHING) != 0;
}

static bool can_take_byte(output_info *oi) {
    return *(oi->output_size) < oi->buf_size;
}


/* Return the longest match for the bytes at buf[end:end+maxlen] between
 * buf[start] and buf[end-1]. If no match is found, return -1. */
static uint_t find_longest_match(heatshrink_encoder* const hse, uint_t start,
        uint_t end, const uint_t maxlen, uint_t& match_length) {
    LOG("-- scanning for match of buf[%u:%u] between buf[%u:%u] (max %u bytes)\n",
        end, end + maxlen, start, end + maxlen - 1, maxlen);

#if !HEATSHRINK_USE_INDEX
    const size_t break_even_point =
      (1 + HEATSHRINK_ENCODER_WINDOW_BITS(hse) +
          HEATSHRINK_ENCODER_LOOKAHEAD_BITS(hse)) / 8;

    if(maxlen <= break_even_point) [[unlikely]] {
        return MATCH_NOT_FOUND;
    }

    uint32_t match_maxlen = 0;
    uint32_t match_index = MATCH_NOT_FOUND;
    {
        const uint8_t* const buf = hse->buffer;

        const uint8_t* const data = buf+start;
        const uint8_t* const pattern = buf+end;
        const uint32_t dataLen = end-start;
        const heatshrink::byte_span lm = heatshrink::Locator::find_longest_match(pattern,maxlen,data,dataLen);

        if(lm.empty()) {
            return MATCH_NOT_FOUND;
        } else {
            match_index = lm.data()-buf;
            match_maxlen = lm.size_bytes();
        }
    }

#else

    const uint8_t* const buf = hse->buffer;

    uint_t match_maxlen = 0;
    uint_t match_index = MATCH_NOT_FOUND;

    uint_t len = 0;
    const uint8_t* const needlepoint = &buf[end];

    struct hs_index *hsi = HEATSHRINK_ENCODER_INDEX(hse);
    int_t pos = hsi->index[end];

    while(pos >= (int_t)start) {
        const uint8_t * const pospoint = &buf[pos];
        len = 0;

        /* Only check matches that will potentially beat the current maxlen.
         * This is redundant with the index if match_maxlen is 0, but the
         * added branch overhead to check if it == 0 seems to be worse. */
        if (pospoint[match_maxlen] != needlepoint[match_maxlen]) {
            pos = hsi->index[pos];
            continue;
        }

        for (len = 1; len < maxlen; len++) {
            if (pospoint[len] != needlepoint[len]) break;
        }
        // len = 1+heatshrink::Locator::cmp8(pospoint+1,needlepoint+1,maxlen-1);

        if (len > match_maxlen) {
            match_maxlen = len;
            match_index = pos;
            if (len == maxlen) { break; } /* won't find better */
        }
        pos = hsi->index[pos];
    }

    const size_t break_even_point =
      (1 + HEATSHRINK_ENCODER_WINDOW_BITS(hse) +
          HEATSHRINK_ENCODER_LOOKAHEAD_BITS(hse)) / 8;

#endif
    /* Instead of comparing break_even_point against 8*match_maxlen,
     * compare match_maxlen against break_even_point/8 to avoid
     * overflow. Since MIN_WINDOW_BITS and MIN_LOOKAHEAD_BITS are 4 and
     * 3, respectively, break_even_point/8 will always be at least 1. */
    if (match_maxlen > break_even_point) {
        LOG("-- best match: %u bytes at -%u\n",
            match_maxlen, end - match_index);
        match_length = match_maxlen;
        return end - match_index;
    }
    LOG("-- none found\n");
    return MATCH_NOT_FOUND;
}

static /* u8 */ uint_t push_outgoing_bits(heatshrink_encoder *hse, output_info *oi) {
    /* u8 */ uint_t count = hse->outgoing_bits_count;
    if(count != 0) {
        hse->outgoing_bits_count = 0;
        uint_t bits = hse->outgoing_bits;
        if (count > 8) {
            bits = bits >> (count - 8);
            hse->outgoing_bits_count = count-8;
            count = 8;
        }
        LOG("-- pushing %d outgoing bits: 0x%02x\n", count, bits);
        push_bits(hse, count, bits, oi);
    }
    return count;
}

/* Push COUNT (max 8) bits to the output buffer, which has room.
 * Bytes are set from the lowest bits, up. */
static void push_bits(heatshrink_encoder *hse, /* u8 */ uint_t count, /* u8 */ uint_t bits,
        output_info *oi) {
    ASSERT(count <= 8);
    LOG("++ push_bits: %d bits, input of 0x%02x\n", count, bits);

    static_assert(BIT_INDEX_INIT == 0 || BIT_INDEX_INIT == 0x80);

    if constexpr (BIT_INDEX_INIT == 0) {
        uint_t bit = hse->bit_index;

        uint_t out = hse->current_byte;
        out = (out << count) | (bits & ((1<<count)-1));
        bit += count;
        if(bit >= 8) {
            oi->buf[(*oi->output_size)++] = out >> (bit-8);
            bit -= 8;
        }
        hse->bit_index = bit;
        hse->current_byte = out;

    } else {
        /* If adding a whole byte and at the start of a new output byte,
        * just push it through whole and skip the bit IO loop. */
        if (count == 8 && hse->bit_index == BIT_INDEX_INIT) {
            oi->buf[(*oi->output_size)++] = bits;
        } else {
            for (int i=count - 1; i>=0; i--) {
                bool bit = bits & (1 << i);
                if (bit) { hse->current_byte |= hse->bit_index; }
                if (0) {
                    LOG("  -- setting bit %d at bit index 0x%02x, byte => 0x%02x\n",
                        bit ? 1 : 0, hse->bit_index, hse->current_byte);
                }
                hse->bit_index >>= 1;
                if (hse->bit_index == 0x00) {
                    hse->bit_index = BIT_INDEX_INIT;
                    LOG(" > pushing byte 0x%02x\n", hse->current_byte);
                    oi->buf[(*oi->output_size)++] = hse->current_byte;
                    hse->current_byte = 0x00;
                }
            }
        }
    }
}

static void push_literal_byte(heatshrink_encoder *hse, output_info *oi) {
    uint_t processed_offset = hse->match_scan_index - 1;
    uint_t input_offset = get_input_offset(hse) + processed_offset;
    /* u8 */ uint_t c = hse->buffer[input_offset];
    LOG("-- yielded literal byte 0x%02x ('%c') from +%d\n",
        c, isprint(c) ? c : '.', input_offset);
    push_bits(hse, 8, c, oi);
}

static void save_backlog(heatshrink_encoder *hse) {
    size_t input_buf_sz = get_input_buffer_size(hse);

    uint_t msi = hse->match_scan_index;

    /* Copy processed data to beginning of buffer, so it can be
     * used for future matches. Don't bother checking whether the
     * input is less than the maximum size, because if it isn't,
     * we're done anyway. */
    uint_t rem = input_buf_sz - msi; // unprocessed bytes
    uint_t shift_sz = input_buf_sz + rem;

    memmove(&hse->buffer[0],
        &hse->buffer[input_buf_sz - rem],
        shift_sz);

    hse->match_scan_index = 0;
    hse->input_size -= input_buf_sz - rem;
}


} // extern "C"

#endif // HEATSHRINK_32BIT
