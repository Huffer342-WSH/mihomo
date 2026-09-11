package memconservative

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func TestDecodeMatchedRecordEOF(t *testing.T) {
	tests := []struct {
		name string
		data []byte
		code string
		want []byte
		err  error
	}{
		{"complete prefix", []byte{10, 4, 10, 2, 'c', 'n'}, "cn", []byte{10, 2, 'c', 'n'}, nil},
		{"complete payload", []byte{10, 6, 10, 2, 'c', 'n', 16, 1}, "cn", []byte{10, 2, 'c', 'n', 16, 1}, nil},
		{"EOF after name", []byte{10, 5, 10, 2, 'c', 'n'}, "cn", nil, errFailedToReadExpectedLenBytes},
		{"partial payload", []byte{10, 6, 10, 2, 'c', 'n', 16}, "cn", nil, errFailedToReadExpectedLenBytes},
		{"missing category", []byte{10, 4, 10, 2, 'c', 'n'}, "us", nil, errCodeNotFound},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "geosite.dat")
			if err := os.WriteFile(path, tt.data, 0600); err != nil {
				t.Fatal(err)
			}
			got, err := Decode(path, tt.code)
			if !errors.Is(err, tt.err) || !bytes.Equal(got, tt.want) {
				t.Fatalf("Decode = (%x, %v), want (%x, %v)", got, err, tt.want, tt.err)
			}
		})
	}
}
