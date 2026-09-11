package trie

import (
	"reflect"
	"strings"
	"testing"

	"github.com/metacubex/mihomo/common/utils"
)

// Compare key bytes and insertion order with the previous construction path.
// Lookup tests alone may miss differences in Unicode normalization or keys
// consumed by DomainMapBuilder before Build.
func TestDomainSetBuilderKeyCompatibility(t *testing.T) {
	labels := []string{"example", "MiXeD", "中文", "École", "İ", "😀", "a\xffb", "\xe2\x82", "*", "a*b", "", "+", " a"}
	patterns := []string{"", ".", "+", "*", "example.com.", " example.com", "example.com\u2003"}
	for _, label := range labels {
		for _, prefix := range []string{"", ".", "+.", "*."} {
			patterns = append(patterns, prefix+label+".Example.COM")
		}
	}
	var builder DomainSetBuilder
	var expected []string
	for _, pattern := range patterns {
		parts, wantErr := ValidAndSplitDomain(pattern)
		err := builder.Insert(pattern)
		if (err == nil) != (wantErr == nil) || err != nil && err.Error() != wantErr.Error() {
			t.Fatalf("%q: error = %v, want %v", pattern, err, wantErr)
		}
		if wantErr == nil {
			if parts[0] == complexWildcard {
				expected = append(expected, utils.Reverse(strings.Join(parts[1:], domainStep)))
			}
			if parts[0] == dotWildcard {
				parts[0] = complexWildcard
			}
			expected = append(expected, utils.Reverse(strings.Join(parts, domainStep)))
		}
		if !reflect.DeepEqual(builder.keys, expected) && !(len(builder.keys) == 0 && len(expected) == 0) {
			t.Fatalf("%q: generated keys differ from previous builder", pattern)
		}
	}
}
