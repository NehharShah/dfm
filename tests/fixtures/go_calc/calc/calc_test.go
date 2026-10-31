package calc

import "testing"

func TestAdd(t *testing.T) {
	if got := Add(2, 3); got != 5 {
		t.Errorf("Add(2, 3) = %d, want 5", got)
	}
}

func TestZero(t *testing.T) {
	cases := map[string][2]int{"zeros": {0, 0}}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			if got := Add(c[0], c[1]); got != 0 {
				t.Errorf("got %d", got)
			}
		})
	}
}
