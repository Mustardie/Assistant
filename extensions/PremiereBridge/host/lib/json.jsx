/**
 * Minimal JSON for ExtendScript.
 *
 * ExtendScript is ES3 and ships no JSON object, but the entire bridge protocol
 * is JSON. This provides just stringify/parse, written to ES3 rules (no
 * Array.isArray, no Object.keys, no trailing commas, no const/let).
 *
 * parse() is a recursive-descent parser rather than an eval() wrapper on
 * purpose: eval would execute whatever arrived over the socket inside
 * Premiere, which is exactly the capability this architecture is designed to
 * withhold from the editing model.
 */
if (typeof JSON === 'undefined') { JSON = {}; }

(function () {
    var ESCAPES = {
        '\b': '\\b', '\t': '\\t', '\n': '\\n',
        '\f': '\\f', '\r': '\\r', '"': '\\"', '\\': '\\\\'
    };

    function quote(text) {
        var out = '"';
        for (var i = 0; i < text.length; i++) {
            var ch = text.charAt(i);
            if (ESCAPES[ch]) {
                out += ESCAPES[ch];
            } else if (ch < ' ') {
                var hex = ch.charCodeAt(0).toString(16);
                out += '\\u' + '0000'.substring(hex.length) + hex;
            } else {
                out += ch;
            }
        }
        return out + '"';
    }

    function isArray(value) {
        return Object.prototype.toString.apply(value) === '[object Array]';
    }

    if (typeof JSON.stringify !== 'function') {
        JSON.stringify = function (value) {
            if (value === null || value === undefined) { return 'null'; }

            var type = typeof value;
            if (type === 'number') {
                return isFinite(value) ? String(value) : 'null';
            }
            if (type === 'boolean') { return String(value); }
            if (type === 'string') { return quote(value); }

            if (isArray(value)) {
                var items = [];
                for (var i = 0; i < value.length; i++) {
                    var item = JSON.stringify(value[i]);
                    items[items.length] = (item === undefined) ? 'null' : item;
                }
                return '[' + items.join(',') + ']';
            }

            if (type === 'object') {
                var pairs = [];
                for (var key in value) {
                    if (Object.prototype.hasOwnProperty.call(value, key)) {
                        var encoded = JSON.stringify(value[key]);
                        if (encoded !== undefined) {
                            pairs[pairs.length] = quote(String(key)) + ':' + encoded;
                        }
                    }
                }
                return '{' + pairs.join(',') + '}';
            }
            return undefined;
        };
    }

    if (typeof JSON.parse !== 'function') {
        JSON.parse = function (text) {
            var at = 0;
            var source = String(text);

            function error(message) {
                throw new Error('JSON parse error at ' + at + ': ' + message);
            }

            function peek() { return source.charAt(at); }

            function whitespace() {
                while (at < source.length) {
                    var ch = source.charAt(at);
                    if (ch === ' ' || ch === '\t' || ch === '\n' || ch === '\r') {
                        at++;
                    } else {
                        break;
                    }
                }
            }

            function expect(ch) {
                if (source.charAt(at) !== ch) { error('expected ' + ch); }
                at++;
            }

            function parseString() {
                expect('"');
                var out = '';
                while (at < source.length) {
                    var ch = source.charAt(at++);
                    if (ch === '"') { return out; }
                    if (ch !== '\\') { out += ch; continue; }
                    var esc = source.charAt(at++);
                    if (esc === 'u') {
                        out += String.fromCharCode(
                            parseInt(source.substr(at, 4), 16));
                        at += 4;
                    } else if (esc === 'b') { out += '\b';
                    } else if (esc === 'f') { out += '\f';
                    } else if (esc === 'n') { out += '\n';
                    } else if (esc === 'r') { out += '\r';
                    } else if (esc === 't') { out += '\t';
                    } else { out += esc; }
                }
                error('unterminated string');
            }

            function parseNumber() {
                var start = at;
                if (peek() === '-') { at++; }
                while (at < source.length && source.charAt(at) >= '0'
                       && source.charAt(at) <= '9') { at++; }
                if (peek() === '.') {
                    at++;
                    while (at < source.length && source.charAt(at) >= '0'
                           && source.charAt(at) <= '9') { at++; }
                }
                if (peek() === 'e' || peek() === 'E') {
                    at++;
                    if (peek() === '+' || peek() === '-') { at++; }
                    while (at < source.length && source.charAt(at) >= '0'
                           && source.charAt(at) <= '9') { at++; }
                }
                var value = Number(source.substring(start, at));
                if (isNaN(value)) { error('bad number'); }
                return value;
            }

            function parseValue() {
                whitespace();
                var ch = peek();
                if (ch === '{') {
                    at++;
                    var object = {};
                    whitespace();
                    if (peek() === '}') { at++; return object; }
                    for (;;) {
                        whitespace();
                        var key = parseString();
                        whitespace();
                        expect(':');
                        object[key] = parseValue();
                        whitespace();
                        if (peek() === ',') { at++; continue; }
                        expect('}');
                        return object;
                    }
                }
                if (ch === '[') {
                    at++;
                    var array = [];
                    whitespace();
                    if (peek() === ']') { at++; return array; }
                    for (;;) {
                        array[array.length] = parseValue();
                        whitespace();
                        if (peek() === ',') { at++; continue; }
                        expect(']');
                        return array;
                    }
                }
                if (ch === '"') { return parseString(); }
                if (source.substr(at, 4) === 'true') { at += 4; return true; }
                if (source.substr(at, 5) === 'false') { at += 5; return false; }
                if (source.substr(at, 4) === 'null') { at += 4; return null; }
                return parseNumber();
            }

            var result = parseValue();
            whitespace();
            if (at < source.length) { error('trailing characters'); }
            return result;
        };
    }
}());
