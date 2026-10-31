import Testing
@testable import Calc

@Test func addsTwoNumbers() {
    #expect(add(2, 3) == 5)
}

@Test func handlesZero() {
    #expect(add(0, 0) == 0)
}
